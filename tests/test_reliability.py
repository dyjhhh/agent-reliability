from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from reliability import AlertGate, Policy, job_health


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite"
        self.gate = AlertGate(self.path)
        self.addCleanup(self.gate.close)

    def observe(self, name, at=2000, severity="yellow", **kwargs):
        return self.gate.observe(name, "test", severity, at, kwargs.pop("uptime", at), **kwargs)

    def ready(self):
        self.observe("first")
        return self.observe("second", 2180)

    def test_boot_and_host_failures_are_not_confirmation_evidence(self):
        self.assertEqual(self.observe("boot", 100)["reason"], "boot-grace")
        self.assertEqual(self.observe("host", 2000, host_healthy=False)["reason"], "host-degraded")
        self.assertEqual(self.observe("healthy", 2300)["reason"], "confirming")

    def test_confirmations_need_separate_intervals(self):
        self.observe("one")
        self.assertEqual(self.observe("two", 2001)["reason"], "confirming")
        self.assertEqual(self.observe("three", 2180)["decision"], "page")

    def test_confirmation_window_expires(self):
        self.observe("one")
        self.assertEqual(self.observe("two", 24000)["reason"], "confirming")

    def test_failed_send_allows_retry_but_pending_attempt_does_not(self):
        self.ready()
        self.assertEqual(self.observe("wait", 2181)["reason"], "delivery-pending")
        self.gate.acknowledge("second", False, 2182)
        self.assertEqual(self.observe("retry", 2183)["decision"], "page")

    def test_success_dedupes_and_red_escalates(self):
        self.ready()
        self.gate.acknowledge("second", True, 2181)
        self.assertEqual(self.observe("same", 2182)["reason"], "duplicate")
        self.assertEqual(self.observe("worse", 2183, "red")["decision"], "page")

    def test_recovery_cancels_pending_attempt_and_resets_confirmation(self):
        self.ready()
        self.assertEqual(self.observe("clear", 2181, "clear")["reason"], "recovered")
        with self.assertRaises(ValueError):
            self.gate.acknowledge("second", True, 2182)
        self.assertEqual(self.observe("new", 2400)["reason"], "confirming")

    def test_duplicate_event_is_idempotent_and_changed_payload_is_rejected(self):
        expected = self.observe("one")
        self.assertEqual(self.observe("one"), expected)
        with self.assertRaises(ValueError):
            self.observe("one", 2001)
        self.assertEqual(self.gate.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_stale_and_invalid_inputs_do_not_write(self):
        self.observe("one")
        for args in [("late", 1999, "yellow"), ("invalid", 2300, "green"), ("nan", float("nan"), "red")]:
            with self.assertRaises(ValueError):
                self.observe(*args)
        self.assertEqual(self.gate.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_restart_preserves_pending_outbox_and_policy(self):
        self.ready()
        other = AlertGate(self.path)
        try:
            self.assertEqual(other.observe("next", "test", "yellow", 2181, 2181)["reason"], "delivery-pending")
        finally:
            other.close()
        with self.assertRaises(ValueError):
            AlertGate(self.path, Policy(confirmations=3))

    def test_acknowledgement_retries_are_exact(self):
        self.ready()
        self.gate.acknowledge("second", True, 2181)
        self.gate.acknowledge("second", True, 2181)
        with self.assertRaises(ValueError):
            self.gate.acknowledge("second", False, 2181)

    def test_concurrent_observers_create_one_pending_attempt(self):
        self.observe("first")
        def observe(name):
            gate = AlertGate(self.path)
            try:
                return gate.observe(name, "test", "yellow", 2180, 2180)["reason"]
            finally:
                gate.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            reasons = list(pool.map(observe, ["worker-a", "worker-b"]))
        self.assertCountEqual(reasons, ["eligible", "delivery-pending"])


class HealthTests(unittest.TestCase):
    def test_missing_evidence_is_unknown_not_healthy(self):
        self.assertEqual(job_health(1000, None, 300), "unknown")

    def test_period_and_grace_are_respected(self):
        self.assertEqual(job_health(1000, 600, 300, 100), "healthy")
        self.assertEqual(job_health(1001, 600, 300, 100), "stale")

    def test_recent_failure_and_clock_errors(self):
        self.assertEqual(job_health(1000, 900, 300, last_failure=950), "failed")
        self.assertEqual(job_health(1000, 1100, 300), "unknown-clock")
        with self.assertRaises(ValueError):
            job_health(1000, 900, 0)


if __name__ == "__main__":
    unittest.main()
