"""Offline alert decisions. No network, credentials, processes, or background jobs.

The SQLite outbox separates eligibility from confirmed delivery. A caller must
explicitly acknowledge an attempt; making a decision never means a page was sent.
"""
import hashlib
import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass


def seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("time must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError("time must be finite and nonnegative")
    return value


@dataclass(frozen=True)
class Policy:
    confirmations: int = 2
    minimum_spacing: int = 180
    confirmation_window: int = 21600
    dedupe_window: int = 21600
    boot_grace: int = 1200

    def __post_init__(self):
        if type(self.confirmations) is not int or self.confirmations < 1:
            raise ValueError("confirmations must be a positive integer")
        for name in ("minimum_spacing", "confirmation_window", "dedupe_window", "boot_grace"):
            seconds(getattr(self, name))
        if self.minimum_spacing <= 0 or self.confirmation_window <= 0:
            raise ValueError("confirmation timing must be positive")
        if (self.confirmations - 1) * self.minimum_spacing > self.confirmation_window:
            raise ValueError("confirmation window is too short")


class AlertGate:
    def __init__(self, database, policy=Policy()):
        self.policy = policy
        self.db = sqlite3.connect(database, timeout=5, isolation_level=None)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, policy TEXT);
            CREATE TABLE IF NOT EXISTS states (class TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, class TEXT NOT NULL,
                at REAL NOT NULL, decision TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, class TEXT NOT NULL, severity TEXT NOT NULL,
                at REAL NOT NULL, status TEXT NOT NULL, acknowledged_at REAL);
        """)
        expected = json.dumps(asdict(policy), sort_keys=True)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT policy FROM config WHERE id=1").fetchone()
            if row and row[0] != expected:
                raise ValueError("existing database uses a different policy")
            self.db.execute("INSERT OR IGNORE INTO config VALUES (1, ?)", (expected,))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _load(self, category):
        row = self.db.execute("SELECT body FROM states WHERE class=?", (category,)).fetchone()
        return json.loads(row[0]) if row else {"samples": [], "seen": 0}

    def _save(self, category, state):
        self.db.execute("INSERT OR REPLACE INTO states VALUES (?, ?)",
                        (category, json.dumps(state, sort_keys=True)))

    def observe(self, event_id, category, severity, at, uptime, host_healthy=True):
        for value in (event_id, category):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                raise ValueError("event and class identifiers must be short labels")
        if severity not in ("clear", "yellow", "red") or type(host_healthy) is not bool:
            raise ValueError("invalid severity or host health")
        seconds(at)
        seconds(uptime)
        payload = [event_id, category, severity, at, uptime, host_healthy]
        digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT digest, decision FROM events WHERE id=?", (event_id,)).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError("event ID was reused with different input")
                self.db.execute("COMMIT")
                return json.loads(old[1])
            state = self._load(category)
            if at < state["seen"]:
                raise ValueError("out-of-order observation")
            state["seen"] = at
            if severity == "clear":
                state = {"samples": [], "seen": at}
                self.db.execute("UPDATE outbox SET status='cancelled' WHERE class=? AND status='pending'", (category,))
                reason = "recovered"
            elif uptime < self.policy.boot_grace or not host_healthy:
                state["samples"] = []
                reason = "boot-grace" if uptime < self.policy.boot_grace else "host-degraded"
            else:
                samples = [t for t in state["samples"] if at - t <= self.policy.confirmation_window]
                if not samples or at - samples[-1] >= self.policy.minimum_spacing:
                    samples.append(at)
                state["samples"] = samples[-self.policy.confirmations:]
                last = state.get("delivered")
                pending = self.db.execute("SELECT id FROM outbox WHERE class=? AND status='pending'", (category,)).fetchone()
                if len(samples) < self.policy.confirmations:
                    reason = "confirming"
                elif pending:
                    reason = "delivery-pending"
                elif last and at - last["at"] < self.policy.dedupe_window and not (last["severity"] == "yellow" and severity == "red"):
                    reason = "duplicate"
                else:
                    reason = "eligible"
                    self.db.execute("INSERT INTO outbox VALUES (?, ?, ?, ?, 'pending', NULL)",
                                    (event_id, category, severity, at))
            result = {"event_id": event_id, "decision": "page" if reason == "eligible" else "suppress", "reason": reason}
            self._save(category, state)
            self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                            (event_id, digest, category, at, json.dumps(result)))
            self.db.execute("COMMIT")
            return result
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def acknowledge(self, event_id, delivered, at):
        """Record a caller-reported delivery result. This is not proof from a provider."""
        if type(delivered) is not bool:
            raise ValueError("delivered must be boolean")
        seconds(at)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT class, severity, at, status, acknowledged_at FROM outbox WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise ValueError("no delivery attempt for this event")
            category, severity, observed_at, status, acknowledged = row
            target = "delivered" if delivered else "failed"
            if status != "pending":
                if status != target or acknowledged != at:
                    raise ValueError("conflicting or cancelled acknowledgement")
                self.db.execute("COMMIT")
                return
            if at < observed_at:
                raise ValueError("delivery predates observation")
            state = self._load(category)
            if delivered:
                state["delivered"] = {"at": at, "severity": severity}
            state["seen"] = max(state["seen"], at)
            self._save(category, state)
            self.db.execute("UPDATE outbox SET status=?, acknowledged_at=? WHERE id=?", (target, at, event_id))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise


def job_health(now, last_success, expected_every, grace=0, last_failure=None):
    """Use successful-work evidence, never a process's existence, for freshness."""
    for value in (now, expected_every, grace):
        seconds(value)
    if expected_every == 0:
        raise ValueError("expected period must be positive")
    for value in (last_success, last_failure):
        if value is not None:
            seconds(value)
            if value > now:
                return "unknown-clock"
    if last_success is None:
        return "unknown"
    if last_failure is not None and last_failure >= last_success:
        return "failed"
    return "stale" if now - last_success > expected_every + grace else "healthy"
