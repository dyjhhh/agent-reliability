"""Delivery reconciliation for a job that polls an inbox and forwards each new message.

The inbox and the sender are caller objects and time is passed in, so every failure path runs
offline. Nothing here opens a network connection or sends anything itself.

    inbox.list(since)       -> iterable of (message_id, arrived_at) for messages after `since`
    inbox.fetch(message_id) -> the message body; may raise, a timeout for example
    send(message_id, body)  -> True only when the transport confirmed the send

Three rules. A message is marked seen only after its body was fetched and `send` returned True,
in the same transaction that records the delivery in the ledger. A failed list call is not an
empty inbox: it changes no state, so the next poll still looks back to the last real success. And
`audit` compares the inbox itself with the ledger, because "the list call worked" is not evidence
that a message arrived. A message the audit finds missing is re-queued; it is escalated once if it
is still undelivered a grace period later.
"""
import json
import re
import sqlite3
from dataclasses import asdict, dataclass

from reliability import seconds

_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")


@dataclass(frozen=True)
class DeliveryPolicy:
    lookback: int = 86400        # a poll lists at least this far back, further after an outage
    settle: int = 900            # how long a message may sit before a successful poll must handle it
    audit_window: int = 259200   # how far back the audit compares the inbox with the ledger
    grace: int = 72000           # how long a re-queued message may stay undelivered before escalation

    def __post_init__(self):
        for name, value in asdict(self).items():
            if seconds(value) == 0:
                raise ValueError(f"{name} must be positive")


def _rows(listing):
    rows = []
    for row in listing:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError("each inbox row must be (message_id, arrived_at)")
        message_id, arrived = row
        if not isinstance(message_id, str) or not _ID.fullmatch(message_id):
            raise ValueError("message identifiers must be short labels")
        rows.append((message_id, seconds(arrived)))
    return sorted(rows, key=lambda row: (row[1], row[0]))


class DeliveryLedger:
    """SQLite state for one poller: seen ids, the delivery ledger, the re-delivery queue, the marker."""

    def __init__(self, database, started_at, policy=DeliveryPolicy()):
        seconds(started_at)
        self.policy = policy
        self.db = sqlite3.connect(database, timeout=5, isolation_level=None)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                id INTEGER PRIMARY KEY CHECK (id = 1), policy TEXT NOT NULL, started_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS ledger (id TEXT PRIMARY KEY, delivered_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS queue (
                id TEXT PRIMARY KEY, arrived_at REAL NOT NULL, queued_at REAL NOT NULL, escalated_at REAL);
            CREATE TABLE IF NOT EXISTS marker (id INTEGER PRIMARY KEY CHECK (id = 1), at REAL NOT NULL);
        """)
        expected = json.dumps(asdict(policy), sort_keys=True)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT policy, started_at FROM config").fetchone()
            if row and row[0] != expected:
                raise ValueError("existing database uses a different policy")
            if not row:
                self.db.execute("INSERT INTO config VALUES (1, ?, ?)", (expected, started_at))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            self.db.close()
            raise
        # The ledger cannot vouch for messages from before it existed; on reopen the stored start wins.
        self.started_at = row[1] if row else started_at

    def close(self):
        self.db.close()

    def marker(self):
        row = self.db.execute("SELECT at FROM marker").fetchone()
        return row[0] if row else None

    def _ids(self, table):
        return {row[0] for row in self.db.execute(f"SELECT id FROM {table}")}

    def _check_clock(self, now):
        seconds(now)
        last = self.marker()
        if last is not None and now < last:
            raise ValueError("clock is earlier than the last successful poll")

    def poll(self, inbox, send, now):
        """Fetch and forward every unseen message. Returns what happened; never guesses success."""
        self._check_clock(now)
        last = self.marker()
        since = now - self.policy.lookback if last is None else min(now - self.policy.lookback, last)
        since = max(since, self.started_at)
        try:
            listed = _rows(inbox.list(since))
        except Exception as error:
            # A failed list is not an empty inbox. The marker stays put, so the next poll's window
            # still reaches back to the last poll that really saw the inbox.
            return {"status": "list-failed", "error": type(error).__name__,
                    "delivered": [], "fetch_failed": [], "send_failed": []}
        seen = self._ids("seen")
        candidates = {message_id: arrived for message_id, arrived in listed if message_id not in seen}
        # Re-queued messages are retried even when they are older than this poll's window.
        for message_id, arrived in self.db.execute("SELECT id, arrived_at FROM queue"):
            candidates.setdefault(message_id, arrived)
        report = {"status": "ok", "delivered": [], "fetch_failed": [], "send_failed": []}
        for message_id in sorted(candidates, key=lambda key: (candidates[key], key)):
            try:
                body = inbox.fetch(message_id)
            except Exception:
                body = None
            if not isinstance(body, str) or not body:
                report["fetch_failed"].append(message_id)   # left unseen; the next poll retries it
                continue
            try:
                confirmed = send(message_id, body) is True
            except Exception:
                confirmed = False
            if not confirmed:
                report["send_failed"].append(message_id)    # not delivered, so not seen either
                continue
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (message_id,))
                self.db.execute("INSERT OR IGNORE INTO ledger VALUES (?, ?)", (message_id, now))
                self.db.execute("DELETE FROM queue WHERE id=?", (message_id,))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            report["delivered"].append(message_id)
        self.db.execute("INSERT OR REPLACE INTO marker VALUES (1, ?)", (now,))
        return report

    def audit(self, inbox, now):
        """Compare the inbox with the ledger; re-queue what is missing, escalate what stays missing.

        A message is expected in the ledger when it arrived after the ledger started, inside the
        audit window, and at least `settle` seconds before the last successful poll. A missing one
        is queued and removed from the seen set, so the next poll fetches and sends it. A queued
        message still undelivered `grace` seconds after it was queued is returned once in
        `escalate`; it stays queued, so polls keep retrying it. Escalation is a returned decision:
        nothing is sent from here.
        """
        self._check_clock(now)
        last = self.marker()
        if last is None:
            return {"status": "no-successful-poll", "requeued": [], "escalate": []}
        low = max(self.started_at, now - self.policy.audit_window)
        high = last - self.policy.settle
        try:
            rows = _rows(inbox.list(low))
        except Exception as error:
            return {"status": "list-failed", "error": type(error).__name__, "requeued": [], "escalate": []}
        delivered, queued = self._ids("ledger"), self._ids("queue")
        missing = [(message_id, arrived) for message_id, arrived in rows
                   if low < arrived <= high and message_id not in delivered and message_id not in queued]
        overdue = [row[0] for row in self.db.execute(
            "SELECT id FROM queue WHERE escalated_at IS NULL AND ? - queued_at >= ? ORDER BY arrived_at, id",
            (now, self.policy.grace))]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for message_id, arrived in missing:
                self.db.execute("INSERT INTO queue VALUES (?, ?, ?, NULL)", (message_id, arrived, now))
                self.db.execute("DELETE FROM seen WHERE id=?", (message_id,))
            for message_id in overdue:
                self.db.execute("UPDATE queue SET escalated_at=? WHERE id=?", (now, message_id))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return {"status": "ok", "requeued": [row[0] for row in missing], "escalate": overdue}
