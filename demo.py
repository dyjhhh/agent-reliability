"""Synthetic timeline, explicit clock, temporary database, no messages sent."""
from pathlib import Path
from tempfile import TemporaryDirectory
from delivery import DeliveryLedger
from listener import check
from reliability import AlertGate, job_health


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


def main():
    with TemporaryDirectory() as temp:
        alert_demo(temp)
        delivery_demo(temp)
    listener_demo()
    print("Delivery acknowledgements and sends above are simulated. No notification was sent.")


if __name__ == "__main__":
    main()
