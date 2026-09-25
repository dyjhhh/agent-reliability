from datetime import datetime, timezone
import unittest
from schedule import audit, calendar_period, due_slots

DAY = 86400
HOUR = 3600
NOW = 1000 * DAY


class DueSlotTests(unittest.TestCase):
    def test_weekday_zero_and_seven_both_mean_sunday(self):
        sunday, monday = datetime(2026, 1, 4, 6, 30), datetime(2026, 1, 5, 6, 30)
        self.assertEqual(sunday.isoweekday(), 7)
        for weekday in (0, 7):
            spec = {"Weekday": weekday, "Hour": 6, "Minute": 0}
            self.assertEqual(due_slots(spec, sunday), ["2026-01-04T06:00"])
            self.assertEqual(due_slots(spec, monday), [])

    def test_missing_hour_means_every_hour_and_each_slot_fires_once(self):
        spec = {"Minute": 15}
        due = due_slots(spec, datetime(2026, 1, 5, 10, 20))
        self.assertEqual(due, ["2026-01-05T09:15", "2026-01-05T10:15"])
        self.assertEqual(due_slots(spec, datetime(2026, 1, 5, 10, 20), fired=due), [])
        next_day = due_slots(spec, datetime(2026, 1, 6, 10, 20), fired=due)
        self.assertEqual(next_day, ["2026-01-06T09:15", "2026-01-06T10:15"])

    def test_catch_up_window_is_two_hours(self):
        spec = {"Hour": 6, "Minute": 0}
        self.assertEqual(due_slots(spec, datetime(2026, 1, 5, 5, 59)), [])
        self.assertEqual(due_slots(spec, datetime(2026, 1, 5, 7, 59)), ["2026-01-05T06:00"])
        self.assertEqual(due_slots(spec, datetime(2026, 1, 5, 8, 0)), [])

    def test_month_and_day_restrict_the_date(self):
        spec = {"Month": 3, "Day": 1, "Hour": 9, "Minute": 0}
        self.assertEqual(due_slots(spec, datetime(2026, 3, 1, 9, 30)), ["2026-03-01T09:00"])
        self.assertEqual(due_slots(spec, datetime(2026, 3, 2, 9, 30)), [])
        self.assertEqual(due_slots(spec, datetime(2026, 4, 1, 9, 30)), [])

    def test_catch_up_crosses_midnight(self):
        spec = {"Hour": 23, "Minute": 30}
        self.assertEqual(due_slots(spec, datetime(2026, 1, 6, 0, 45)), ["2026-01-05T23:30"])

    def test_ambiguous_or_invalid_specs_are_rejected(self):
        now = datetime(2026, 1, 5, 6, 30)
        for spec in ({"Hour": 6}, {"Day": 1, "Weekday": 1, "Minute": 0}, {"Weekday": 8, "Minute": 0},
                     {"Minute": True}, {"Minute": 0, "Second": 1}, []):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                due_slots(spec, now)
        with self.assertRaises(ValueError):
            due_slots({"Minute": 0}, now.replace(tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            due_slots({"Minute": 0}, now, window=0)

    def test_calendar_period_is_the_longest_gap(self):
        self.assertEqual(calendar_period([{"Hour": 6, "Minute": 0}, {"Hour": 7, "Minute": 0}]), 23 * HOUR)
        weekdays = [{"Weekday": day, "Hour": 8, "Minute": 0} for day in range(1, 6)]
        self.assertEqual(calendar_period(weekdays), 72 * HOUR)
        self.assertEqual(calendar_period({"Weekday": 1, "Hour": 6, "Minute": 0}), 7 * DAY)


class AuditTests(unittest.TestCase):
    def verdict(self, **fields):
        record = {"name": "job", "kind": "calendar", "period": DAY, "installed_at": 0}
        record.update(fields)
        return audit([record], NOW)[0]

    def test_zero_byte_log_is_not_evidence_of_a_run(self):
        empty = self.verdict(evidence=[{"kind": "log", "at": NOW - 60, "size": 0}])
        self.assertEqual((empty["verdict"], empty["evidence"]), ("NO-EVIDENCE", "none"))
        written = self.verdict(evidence=[{"kind": "log", "at": NOW - 60, "size": 120}])
        self.assertEqual((written["verdict"], written["evidence"]), ("OK", "log"))

    def test_new_job_is_not_yet_due_within_its_first_period(self):
        annual = calendar_period({"Month": 5, "Day": 1, "Hour": 9, "Minute": 0})
        self.assertEqual(self.verdict(period=annual, installed_at=NOW - 42 * DAY)["verdict"], "NOT-YET-DUE")
        self.assertEqual(self.verdict(period=annual, installed_at=NOW - 400 * DAY)["verdict"], "NO-EVIDENCE")

    def test_interval_job_is_stale_after_three_periods(self):
        def run(hours_ago, **fields):
            evidence = [{"kind": "artifact", "at": NOW - hours_ago * HOUR}]
            return self.verdict(kind="interval", period=HOUR, evidence=evidence, **fields)["verdict"]
        self.assertEqual(run(2.5), "OK")
        self.assertEqual(run(3.5), "STALE")
        self.assertEqual(run(3.5, loaded=True, last_exit=0), "STALE")

    def test_a_job_that_genuinely_stopped_is_still_reported_stale(self):
        stopped = self.verdict(loaded=True, last_exit=0, evidence=[
            {"kind": "fire", "at": NOW - 5 * DAY},
            {"kind": "log", "at": NOW - 60, "size": 0},
        ])
        self.assertEqual((stopped["verdict"], stopped["evidence"], stopped["age"]), ("STALE", "fire", 5 * DAY))

    def test_declared_exit_code_is_not_a_failure(self):
        fresh = [{"kind": "fire", "at": NOW - HOUR}]
        self.assertEqual(self.verdict(evidence=fresh, last_exit=3, designed_exits=[3])["verdict"], "OK")
        self.assertEqual(self.verdict(evidence=fresh, last_exit=3)["verdict"], "FAILED")
        self.assertEqual(self.verdict(evidence=fresh, last_exit=1, designed_exits=[3])["verdict"], "FAILED")

    def test_scheduler_state_alone_is_unknown_not_ok(self):
        loaded = self.verdict(kind="interval", period=HOUR, loaded=True, last_exit=0)
        self.assertEqual((loaded["verdict"], loaded["evidence"]), ("UNKNOWN", "scheduler-state"))
        self.assertEqual(self.verdict(kind="interval", period=HOUR)["verdict"], "NO-EVIDENCE")

    def test_clock_errors_and_bad_records(self):
        future = self.verdict(evidence=[{"kind": "fire", "at": NOW + 60}])
        self.assertEqual(future["verdict"], "UNKNOWN-CLOCK")
        job = {"name": "job", "kind": "calendar", "period": DAY}
        for records in ([job, dict(job)], [dict(job, evidence=[{"kind": "log", "at": NOW}])],
                        [dict(job, kind="daily")], [dict(job, period=0)]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                audit(records, NOW)


if __name__ == "__main__":
    unittest.main()
