import unittest
from acceptance import classify, decide

DAY = 86400
NOW = 100 * DAY
VALIDITY = {"phone_alarm": 4 * DAY, "desktop_alarm": 4 * DAY, "text_message": 14 * DAY}


def passed(age):
    return {"status": "PASS", "observed_at": NOW - age, "evidence": "synthetic observation"}


def receipt(**items):
    base = {name: passed(DAY) for name in VALIDITY}
    base.update(items)
    return base


def run(results_list, state=None):
    state, out = state or {}, []
    for results in results_list:
        row = decide(results, state)
        state = row["state"]
        out.append(row)
    return out


class ClassifyTests(unittest.TestCase):
    def test_each_item_has_its_own_window(self):
        results = classify(receipt(phone_alarm=passed(5 * DAY), text_message=passed(5 * DAY)), NOW, VALIDITY)
        self.assertEqual(results, {"desktop_alarm": "pass", "phone_alarm": "expired", "text_message": "pass"})

    def test_missing_incomplete_and_future_evidence_are_not_a_pass(self):
        results = classify({"phone_alarm": {"status": "PASS", "observed_at": NOW - 60},
                            "desktop_alarm": {"status": "PASS", "observed_at": NOW + 60, "evidence": "x"}},
                           NOW, VALIDITY)
        self.assertEqual(results, {"desktop_alarm": "not_passed", "phone_alarm": "not_passed",
                                   "text_message": "never_observed"})

    def test_an_explicit_failure_stays_failed_however_old_it_is(self):
        for status in ("FAILED", "fail", "NOT_HEARD", "NO_SOUND"):
            with self.subTest(status=status):
                old = {"status": status, "observed_at": NOW - 30 * DAY, "evidence": "x"}
                self.assertEqual(classify(receipt(phone_alarm=old), NOW, VALIDITY)["phone_alarm"], "failed")

    def test_an_unreadable_receipt_is_unknown(self):
        self.assertIsNone(classify(None, NOW, VALIDITY))

    def test_invalid_settings_are_rejected(self):
        for args in (("x", NOW, VALIDITY), ({}, NOW, {}), ({}, NOW, {"a": 0}), ({}, -1, VALIDITY),
                     ({}, NOW, {"a": True})):
            with self.subTest(args=args), self.assertRaises(ValueError):
                classify(*args)


class DecideTests(unittest.TestCase):
    def test_expired_evidence_for_32_hours_sends_one_reminder_and_never_pages(self):
        # Every five minutes for 32 hours, the same expired observation is re-read.
        expired = classify(receipt(phone_alarm=passed(5 * DAY)), NOW, VALIDITY)
        rows = run([expired] * (32 * 12))
        self.assertEqual(sum(len(row["notices"]) for row in rows), 1)
        self.assertEqual(rows[0]["notices"][0]["kind"], "retest_due")
        self.assertIn("phone_alarm expired", rows[0]["notices"][0]["text"])
        self.assertFalse(any(row["page"] for row in rows))

    def test_a_failed_retest_pages_every_time_and_is_not_a_cleared_notice(self):
        expired = classify(receipt(phone_alarm=passed(5 * DAY)), NOW, VALIDITY)
        failed = classify(receipt(phone_alarm={"status": "FAILED", "observed_at": NOW, "evidence": "x"}),
                          NOW, VALIDITY)
        rows = run([expired, failed, failed])
        self.assertEqual([row["page"] for row in rows], [[], ["phone_alarm"], ["phone_alarm"]])
        self.assertEqual([row["decision"] for row in rows], ["retest_due", "page", "page"])
        self.assertFalse(any(n["kind"] == "retest_cleared" for row in rows for n in row["notices"]))

    def test_cleared_only_when_every_announced_item_passes_again(self):
        both = classify(receipt(phone_alarm=passed(5 * DAY), desktop_alarm=passed(5 * DAY)), NOW, VALIDITY)
        one = classify(receipt(desktop_alarm=passed(5 * DAY)), NOW, VALIDITY)
        fresh = classify(receipt(), NOW, VALIDITY)
        rows = run([both, one, fresh, fresh])
        self.assertEqual([row["decision"] for row in rows], ["retest_due", "no_change", "retest_cleared", "no_change"])
        self.assertEqual(rows[2]["notices"][0]["items"], ["desktop_alarm", "phone_alarm"])
        self.assertEqual(rows[3]["state"], {"announced": []})

    def test_a_flapping_item_is_announced_once_per_cycle(self):
        phone = classify(receipt(phone_alarm=passed(5 * DAY), desktop_alarm=passed(5 * DAY)), NOW, VALIDITY)
        only_desktop = classify(receipt(desktop_alarm=passed(5 * DAY)), NOW, VALIDITY)
        rows = run([phone, only_desktop, phone, only_desktop])
        self.assertEqual(sum(len(row["notices"]) for row in rows), 1)

    def test_a_new_item_falling_due_sends_a_second_reminder_naming_it(self):
        first = classify(receipt(phone_alarm=passed(5 * DAY)), NOW, VALIDITY)
        second = classify(receipt(phone_alarm=passed(5 * DAY), text_message=passed(15 * DAY)), NOW, VALIDITY)
        rows = run([first, second])
        self.assertEqual(rows[1]["notices"][0]["new"], ["text_message"])
        self.assertEqual(rows[1]["notices"][0]["items"], ["phone_alarm", "text_message"])

    def test_an_unreadable_receipt_changes_nothing(self):
        expired = classify(receipt(phone_alarm=passed(5 * DAY)), NOW, VALIDITY)
        rows = run([expired, None, None])
        self.assertEqual([row["decision"] for row in rows], ["retest_due", "unknown", "unknown"])
        self.assertEqual(rows[-1]["state"], {"announced": ["phone_alarm"]})
        self.assertFalse(any(row["page"] for row in rows[1:]))


if __name__ == "__main__":
    unittest.main()
