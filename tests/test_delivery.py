from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from delivery import DeliveryLedger, DeliveryPolicy

START = 100000
HOUR = 3600


class FakeInbox:
    """Synthetic inbox. Fetch and list failures are scripted per test."""

    def __init__(self):
        self.messages, self.fetch_failures, self.list_calls = {}, {}, []
        self.list_error = None

    def add(self, message_id, arrived, body="synthetic body"):
        self.messages[message_id] = (arrived, body)

    def list(self, since):
        self.list_calls.append(since)
        if self.list_error:
            raise self.list_error
        return [(key, arrived) for key, (arrived, _) in self.messages.items() if arrived > since]

    def fetch(self, message_id):
        remaining = self.fetch_failures.get(message_id, 0)
        if remaining:
            self.fetch_failures[message_id] = remaining - 1
            raise TimeoutError("synthetic fetch timeout")
        return self.messages[message_id][1]


class FakeSender:
    """Records every attempt. Scripted results are used in order, then every send succeeds."""

    def __init__(self, *results):
        self.results, self.attempts = list(results), []

    def __call__(self, message_id, body):
        self.attempts.append(message_id)
        result = self.results.pop(0) if self.results else True
        if isinstance(result, Exception):
            raise result
        return result


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inbox = FakeInbox()
        self.open()

    def open(self, policy=DeliveryPolicy(), name="delivery.sqlite"):
        self.ledger = DeliveryLedger(Path(self.temp.name) / name, START, policy)
        self.addCleanup(self.ledger.close)

    def ids(self, table):
        return sorted(row[0] for row in self.ledger.db.execute(f"SELECT id FROM {table}"))

    def test_fetch_timeout_leaves_the_message_unseen_and_the_next_poll_delivers_it(self):
        self.inbox.add("m1", START + 100)
        self.inbox.add("m2", START + 110)
        self.inbox.fetch_failures["m2"] = 1
        send = FakeSender()
        first = self.ledger.poll(self.inbox, send, START + 200)
        self.assertEqual((first["delivered"], first["fetch_failed"]), (["m1"], ["m2"]))
        self.assertEqual((self.ids("seen"), self.ids("ledger")), (["m1"], ["m1"]))
        second = self.ledger.poll(self.inbox, send, START + 500)
        self.assertEqual(second["delivered"], ["m2"])
        self.assertEqual(send.attempts, ["m1", "m2"])

    def test_list_failure_does_not_refresh_the_success_marker(self):
        self.open(DeliveryPolicy(lookback=60), "short-lookback.sqlite")
        self.ledger.poll(self.inbox, FakeSender(), START + 200)
        self.inbox.list_error = OSError("synthetic list failure")
        self.inbox.add("during-outage", START + 250)
        failed = self.ledger.poll(self.inbox, FakeSender(), START + 300)
        self.assertEqual((failed["status"], failed["delivered"]), ("list-failed", []))
        self.assertEqual(self.ledger.marker(), START + 200)
        self.inbox.list_error = None
        recovered = self.ledger.poll(self.inbox, FakeSender(), START + 1000)
        # The window reaches back to the last real success, not to the failed poll.
        self.assertEqual(self.inbox.list_calls[-1], START + 200)
        self.assertEqual(recovered["delivered"], ["during-outage"])

    def test_a_failed_send_is_not_recorded_as_delivered(self):
        self.inbox.add("m1", START + 100)
        send = FakeSender(False, None, "sent", ConnectionError("synthetic"))
        for now in (200, 300, 400, 500):
            report = self.ledger.poll(self.inbox, send, START + now)
            self.assertEqual(report["send_failed"], ["m1"])
            self.assertEqual((self.ids("seen"), self.ids("ledger")), ([], []))
        self.assertEqual(self.ledger.poll(self.inbox, send, START + 600)["delivered"], ["m1"])
        self.assertEqual(self.ledger.poll(self.inbox, send, START + 700)["delivered"], [])
        self.assertEqual(send.attempts, ["m1"] * 5)

    def test_audit_finds_exactly_the_message_the_old_ordering_lost(self):
        self.inbox.add("before-start", START - 1000)
        self.inbox.add("delivered", START + 1000)
        self.ledger.poll(self.inbox, FakeSender(), START + 1100)
        # The state the incident ordering left behind: the id was marked seen before its fetch, the
        # fetch timed out, nothing was sent, and later runs skip it as seen while the marker moves on.
        self.inbox.add("lost", START + 2000)
        self.ledger.db.execute("INSERT INTO seen VALUES ('lost')")
        self.assertEqual(self.ledger.poll(self.inbox, FakeSender(), START + 3000)["delivered"], [])
        self.inbox.add("after-last-poll", START + 3500)
        audit = self.ledger.audit(self.inbox, START + 4000)
        self.assertEqual((audit["requeued"], audit["escalate"]), (["lost"], []))
        self.assertNotIn("lost", self.ids("seen"))
        send = FakeSender()
        self.ledger.poll(self.inbox, send, START + 4100)
        self.assertEqual(send.attempts, ["lost", "after-last-poll"])
        self.assertEqual(self.ids("queue"), [])

    def test_a_message_inside_the_settle_margin_is_not_missing_yet(self):
        self.ledger.poll(self.inbox, FakeSender(), START + 2000)
        # Both appear in the listing only after the poll that should have handled them.
        self.inbox.add("inside-settle", START + 1500)
        self.inbox.add("before-settle", START + 1000)
        self.assertEqual(self.ledger.audit(self.inbox, START + 2100)["requeued"], ["before-settle"])

    def test_escalates_once_and_only_after_the_grace_period(self):
        policy = DeliveryPolicy(lookback=HOUR)
        self.open(policy, "short-lookback.sqlite")
        self.inbox.add("stuck", START + 1000)
        self.inbox.fetch_failures["stuck"] = 99
        send = FakeSender()
        self.ledger.poll(self.inbox, send, START + 2000)
        queued_at = START + 3000
        self.assertEqual(self.ledger.audit(self.inbox, queued_at)["requeued"], ["stuck"])
        for now, expected in ((queued_at + policy.grace - 1, []), (queued_at + policy.grace, ["stuck"]),
                              (queued_at + policy.grace + 60, [])):
            self.ledger.poll(self.inbox, send, now - 30)
            audit = self.ledger.audit(self.inbox, now)
            self.assertEqual((audit["requeued"], audit["escalate"]), ([], expected))
        # The fetch recovers. The message is older than this poll's window, so only the queue finds it.
        self.inbox.fetch_failures["stuck"] = 0
        late = queued_at + policy.grace + 120
        self.assertEqual(self.ledger.poll(self.inbox, send, late)["delivered"], ["stuck"])
        self.assertGreater(self.inbox.list_calls[-1], START + 1000)
        self.assertEqual(self.ledger.audit(self.inbox, late + 60), {"status": "ok", "requeued": [], "escalate": []})
        self.assertEqual(send.attempts, ["stuck"])

    def test_rerunning_poll_and_audit_is_idempotent(self):
        self.inbox.add("m1", START + 100)
        self.inbox.add("m2", START + 110)
        self.inbox.fetch_failures["m2"] = 99
        send = FakeSender()
        self.ledger.poll(self.inbox, send, START + 2000)
        self.assertEqual(self.ledger.audit(self.inbox, START + 3000)["requeued"], ["m2"])

        def snapshot():
            return [self.ids(table) for table in ("seen", "ledger", "queue")]
        before = snapshot()
        self.assertEqual(self.ledger.audit(self.inbox, START + 3000)["requeued"], [])
        self.assertEqual(self.ledger.poll(self.inbox, send, START + 3000)["delivered"], [])
        self.assertEqual(self.ledger.poll(self.inbox, send, START + 3000)["delivered"], [])
        self.assertEqual(snapshot(), before)
        self.assertEqual(send.attempts, ["m1"])

    def test_an_audit_without_evidence_gives_no_verdict_and_changes_nothing(self):
        self.inbox.add("m1", START + 100)
        self.assertEqual(self.ledger.audit(self.inbox, START + 2000)["status"], "no-successful-poll")
        self.inbox.fetch_failures["m1"] = 99
        self.ledger.poll(self.inbox, FakeSender(), START + 2000)
        self.inbox.list_error = OSError("synthetic list failure")
        self.assertEqual(self.ledger.audit(self.inbox, START + 3000)["status"], "list-failed")
        self.assertEqual(self.ids("queue"), [])

    def test_bad_rows_clock_errors_and_policy_changes_are_rejected(self):
        self.ledger.poll(self.inbox, FakeSender(), START + 200)
        with self.assertRaises(ValueError):
            self.ledger.poll(self.inbox, FakeSender(), START + 100)
        self.inbox.messages["bad id!"] = (START + 300, "body")
        report = self.ledger.poll(self.inbox, FakeSender(), START + 400)
        self.assertEqual((report["status"], self.ledger.marker()), ("list-failed", START + 200))
        with self.assertRaises(ValueError):
            DeliveryLedger(Path(self.temp.name) / "delivery.sqlite", START, DeliveryPolicy(grace=60))
        with self.assertRaises(ValueError):
            DeliveryPolicy(settle=0)


if __name__ == "__main__":
    unittest.main()
