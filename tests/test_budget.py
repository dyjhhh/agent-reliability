import unittest
from budget import BudgetPolicy, budget, classify_run, record, settle

DAY = "2026-01-05"
WINDOW = "window-0900"
REFUSAL = "Error: You've hit your monthly spend limit. Raise it to continue."


class ClassifyTests(unittest.TestCase):
    def test_a_limit_refusal_that_scored_nothing_is_not_a_failure(self):
        for text in (REFUSAL, "usage limit reached for this key", "Request failed: exceeded the credit limit"):
            with self.subTest(text=text):
                self.assertEqual(classify_run(1, text, queued=20, scored=0), "limit_refused")

    def test_only_a_non_zero_exit_with_nothing_scored_is_a_refusal(self):
        self.assertEqual(classify_run(1, REFUSAL, queued=20, scored=3), "partial")
        self.assertEqual(classify_run(0, REFUSAL, queued=20, scored=0), "failed")
        self.assertEqual(classify_run(1, "Traceback: synthetic crash", queued=20, scored=0), "failed")
        self.assertEqual(classify_run(1, "rate limit, retry later", queued=20, scored=0), "failed")

    def test_complete_needs_the_receipt_a_zero_exit_and_every_item(self):
        self.assertEqual(classify_run(0, "", queued=20, scored=20, complete=True), "complete")
        self.assertEqual(classify_run(0, "", queued=20, scored=19, complete=True), "partial")
        self.assertEqual(classify_run(0, "", queued=20, scored=20), "partial")

    def test_invalid_inputs_are_rejected(self):
        for args in ((True, "", 1, 0), (1, None, 1, 0), (1, "", -1, 0), (1, "", 1, 2), (1, "", 1.0, 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                classify_run(*args)
        for settings in ({"daily_cap": -1}, {"max_attempts": 0}, {"daily_cap": True}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                BudgetPolicy(**settings)
        with self.assertRaises(ValueError):
            record([], DAY, WINDOW, "refused", 1, 0)


class AccountingTests(unittest.TestCase):
    def test_a_refusal_charges_neither_the_cap_nor_an_attempt(self):
        ledger = record([], DAY, WINDOW, classify_run(1, REFUSAL, queued=20, scored=0), 20, 0)
        self.assertEqual(ledger[-1]["charged"], False)
        self.assertEqual(budget(ledger, DAY, WINDOW),
                         {"used": 0, "remaining": 25, "attempts": 0, "refusals_today": 1, "may_run": True})

    def test_a_real_failure_is_charged_and_counts_as_an_attempt(self):
        ledger = record([], DAY, WINDOW, classify_run(1, "synthetic crash", queued=20, scored=0), 20, 0)
        self.assertEqual(budget(ledger, DAY, WINDOW)["used"], 20)
        self.assertEqual(budget(ledger, DAY, WINDOW)["attempts"], 1)

    def test_refusals_then_success_charge_the_batch_once(self):
        ledger = []
        for _ in range(3):
            ledger = record(ledger, DAY, WINDOW, classify_run(1, REFUSAL, queued=20, scored=0), 20, 0)
        ledger = record(ledger, DAY, WINDOW, classify_run(0, "", queued=20, scored=20, complete=True), 20, 20)
        row = budget(ledger, DAY, WINDOW)
        self.assertEqual((row["used"], row["remaining"], row["attempts"], row["refusals_today"]), (20, 5, 1, 3))

    def test_the_attempt_limit_counts_charged_runs_only(self):
        ledger = []
        for _ in range(5):
            ledger = record(ledger, DAY, WINDOW, "limit_refused", 5, 0)
        self.assertTrue(budget(ledger, DAY, WINDOW)["may_run"])
        ledger = record(record(ledger, DAY, WINDOW, "failed", 5, 0), DAY, WINDOW, "failed", 5, 0)
        self.assertFalse(budget(ledger, DAY, WINDOW)["may_run"])
        self.assertTrue(budget(ledger, DAY, "next-window")["may_run"])

    def test_the_daily_cap_is_per_day(self):
        ledger = record([], DAY, WINDOW, "complete", 25, 25)
        self.assertFalse(budget(ledger, DAY, "later-window")["may_run"])
        self.assertEqual(budget(ledger, "2026-01-06", "later-window")["remaining"], 25)


class SettleTests(unittest.TestCase):
    STATE = {"watermark": 1000, "queue": ["item-a", "item-b", "item-c"]}

    def test_a_refusal_keeps_the_queue_and_the_watermark(self):
        self.assertEqual(settle(self.STATE, "limit_refused", 2000), self.STATE)
        self.assertEqual(settle(self.STATE, "failed", 2000), self.STATE)

    def test_only_complete_advances_the_watermark(self):
        self.assertEqual(settle(self.STATE, "complete", 2000, ["item-a", "item-b", "item-c"]),
                         {"watermark": 2000, "queue": []})
        self.assertEqual(settle(self.STATE, "partial", 2000, ["item-a"]),
                         {"watermark": 1000, "queue": ["item-b", "item-c"]})

    def test_settle_does_not_modify_its_input(self):
        state = {"watermark": 1000, "queue": ["item-a"]}
        settle(state, "complete", 2000, ["item-a"])
        self.assertEqual(state, {"watermark": 1000, "queue": ["item-a"]})


if __name__ == "__main__":
    unittest.main()
