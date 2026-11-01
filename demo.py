"""Synthetic timeline, explicit clock, temporary database, no messages sent."""
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from acceptance import classify, decide
from budget import budget, classify_run, record, settle
from delivery import DeliveryLedger
from listener import check
from reliability import AlertGate, job_health
from stores import scheduler_health


def alert_demo(temp):
    gate = AlertGate(Path(temp) / "demo.sqlite")
    try:
        for event_id, at, uptime, healthy in [
            ("boot", 100, 100, True),
            ("host", 1400, 1400, False),
            ("first", 2000, 2000, True),
            ("too-soon", 2060, 2060, True),
            ("confirmed", 2180, 2180, True),
        ]:
            row = gate.observe(event_id, "demo-job", "yellow", at, uptime, healthy)
            print(f"{event_id:12} {row['decision']:8} {row['reason']}")
        gate.acknowledge("confirmed", delivered=False, at=2181)
        row = gate.observe("retry", "demo-job", "yellow", 2182, 2182)
        print(f"{'retry':12} {row['decision']:8} {row['reason']} (failed delivery did not silence it)")
        gate.acknowledge("retry", delivered=True, at=2183)
        for event_id, severity, at in [("duplicate", "yellow", 2184), ("escalation", "red", 2185), ("recovery", "clear", 2186)]:
            row = gate.observe(event_id, "demo-job", severity, at, at)
            print(f"{event_id:12} {row['decision']:8} {row['reason']}")
    finally:
        gate.close()
    print("Job evidence:", job_health(4000, None, 600), "/", job_health(4000, 1000, 600))


class DemoInbox:
    def __init__(self):
        self.messages = {"note-1": 1000, "note-2": 1010, "note-3": 1020}
        self.timeouts = {"note-2": 1, "note-3": 99}
        self.list_fails = False

    def list(self, since):
        if self.list_fails:
            raise OSError("synthetic list failure")
        return [(key, arrived) for key, arrived in self.messages.items() if arrived > since]

    def fetch(self, message_id):
        if self.timeouts.get(message_id, 0):
            self.timeouts[message_id] -= 1
            raise TimeoutError("synthetic fetch timeout")
        return "synthetic body"


def delivery_demo(temp):
    print("\nDelivery reconciliation (synthetic inbox; the sender is a stub that only records)")
    inbox, sent = DemoInbox(), []

    def send(message_id, body):
        sent.append(message_id)
        return True
    ledger = DeliveryLedger(Path(temp) / "delivery.sqlite", started_at=0)
    try:
        def poll(label, now):
            row = ledger.poll(inbox, send, now)
            print(f"{label:12} {row['status']:12} delivered={row['delivered']} fetch_failed={row['fetch_failed']}")
        poll("poll", 2000)
        inbox.list_fails = True
        poll("poll", 3800)
        print(f"{'':12} success marker still at {ledger.marker():.0f}, so the next window reaches back to it")
        inbox.list_fails = False
        poll("poll", 5600)
        for label, now in (("audit", 9000), ("audit", 9000 + 72000)):
            row = ledger.audit(inbox, now)
            print(f"{label:12} {row['status']:12} requeued={row['requeued']} escalate={row['escalate']}")
    finally:
        ledger.close()
    print(f"{'':12} sent {sent}; note-3 never fetched, so it is escalated once instead of marked seen")


def listener_demo():
    print("\nListener ticks (the process is alive on every tick)")
    ticks = [{"alive": True, "pending": 0, "consumed": []}] + \
            [{"alive": True, "pending": 1, "consumed": []}] * 3 + \
            [{"alive": True, "pending": 0, "consumed": [101]},
             {"alive": True, "pending": 0, "consumed": [104]}]
    for number, row in enumerate(check(ticks, last_consumed=100), 1):
        extra = " restart" if row["restart"] else f" missing={row['missing']}" if row["missing"] else ""
        print(f"tick {number:<7} {row['verdict']}{extra}")


def evidence_demo():
    print("\nAcceptance evidence (the phone alarm test is five days old; its window is four)")
    day = 86400
    validity = {"phone_alarm": 4 * day, "desktop_alarm": 4 * day}
    receipt = {"phone_alarm": {"status": "PASS", "observed_at": 95 * day, "evidence": "synthetic"},
               "desktop_alarm": {"status": "PASS", "observed_at": 99 * day, "evidence": "synthetic"}}
    state, sent, pages = {}, 0, 0
    for _ in range(32 * 12):  # every five minutes for 32 hours
        row = decide(classify(receipt, 100 * day, validity), state)
        state, sent, pages = row["state"], sent + len(row["notices"]), pages + len(row["page"])
    print(f"{'384 checks':12} reminders={sent} pages={pages}")
    receipt["phone_alarm"] = {"status": "FAILED", "observed_at": 100 * day, "evidence": "synthetic"}
    row = decide(classify(receipt, 100 * day, validity), state)
    print(f"{'retest':12} {row['decision']} {row['page']} (an explicit failure still pages)")


def store_demo(temp):
    print("\nScheduler stores (an update moved the run record to a new file)")
    old, new = Path(temp) / "state-legacy.db", Path(temp) / "state.db"
    for path, last_run in ((old, 1000), (new, 50000)):
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, last_run_at REAL)")
            conn.execute("INSERT INTO jobs VALUES ('demo-job', 'ACTIVE', ?)", (last_run,))
            conn.commit()
    for label, paths in (("old only", [old]), ("both", [old, new])):
        row = scheduler_health(paths, "demo-job", 50060, 600)
        print(f"{label:12} {row['verdict']:6} from {Path(row['store']).name}")


def budget_demo():
    print("\nDaily cap (a spend-limit refusal, then a complete run)")
    ledger, state = [], {"watermark": 0, "queue": ["item-1", "item-2"]}
    for code, text, scored in ((1, "You've hit your monthly spend limit", 0), (0, "", 2)):
        outcome = classify_run(code, text, queued=2, scored=scored, complete=bool(scored))
        ledger = record(ledger, "day-1", "window-1", outcome, 2, scored)
        state = settle(state, outcome, 3600, ["item-1", "item-2"][:scored])
        row = budget(ledger, "day-1", "window-1")
        print(f"{outcome:13} used={row['used']} attempts={row['attempts']} watermark={state['watermark']} "
              f"queue={len(state['queue'])}")


def main():
    with TemporaryDirectory() as temp:
        alert_demo(temp)
        delivery_demo(temp)
        listener_demo()
        evidence_demo()
        store_demo(temp)
    budget_demo()
    print("Delivery acknowledgements and sends above are simulated. No notification was sent.")


if __name__ == "__main__":
    main()
