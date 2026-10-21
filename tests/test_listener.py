import unittest
from listener import check


def tick(alive=True, pending=0, consumed=()):
    return {"alive": alive, "pending": pending, "consumed": list(consumed)}


def verdicts(results):
    return [result["verdict"] for result in results]


class ListenerTests(unittest.TestCase):
    def test_alive_but_deaf_restarts_only_after_three_consecutive_ticks(self):
        # The process is alive on every tick, so a process-only check calls each one healthy.
        results = check([tick(pending=2)] * 5)
        self.assertEqual(verdicts(results), ["deaf"] * 5)
        self.assertEqual([result["restart"] for result in results], [False, False, True, False, False])
        self.assertEqual([result["deaf_streak"] for result in results], [1, 2, 3, 1, 2])

    def test_any_other_verdict_resets_the_deaf_count(self):
        for interruption in (tick(), tick(pending=None), tick(alive=False), tick(pending=1, consumed=[7])):
            with self.subTest(interruption=interruption):
                results = check([tick(pending=1), tick(pending=1), interruption, tick(pending=1), tick(pending=1)])
                self.assertFalse(any(result["restart"] for result in results))

    def test_a_busy_listener_with_updates_waiting_is_not_deaf(self):
        steady = check([tick(pending=4, consumed=[11]), tick(pending=4, consumed=[12]),
                        tick(pending=4, consumed=[13])], last_consumed=10)
        self.assertEqual(verdicts(steady), ["healthy"] * 3)
        self.assertFalse(any(result["restart"] for result in steady))

    def test_contiguous_ids_after_a_long_silence_show_it_was_quiet(self):
        results = check([tick()] * 50 + [tick(consumed=[101])], last_consumed=100)
        self.assertEqual(verdicts(results), ["quiet"] * 50 + ["healthy"])
        self.assertEqual((results[-1]["missing"], results[-1]["last_consumed"]), (0, 101))

    def test_skipped_ids_are_reported_as_a_gap_and_never_restart(self):
        results = check([tick(consumed=[103, 104]), tick(consumed=[105, 107])], last_consumed=100)
        self.assertEqual(verdicts(results), ["gap", "gap"])
        self.assertEqual([result["missing"] for result in results], [2, 1])
        self.assertFalse(any(result["restart"] for result in results))

    def test_the_first_id_arms_the_check_when_no_last_id_is_known(self):
        self.assertEqual(verdicts(check([tick(consumed=[42, 43])])), ["healthy"])
        self.assertEqual(check([tick(consumed=[42, 44])])[0]["missing"], 1)

    def test_renumbered_ids_are_inconclusive_and_the_check_rearms(self):
        results = check([tick(consumed=[7]), tick(consumed=[8]), tick(consumed=[5000]), tick(consumed=[5001])],
                        last_consumed=500)
        self.assertEqual(verdicts(results), ["inconclusive", "healthy", "inconclusive", "healthy"])
        self.assertEqual(results[-1]["last_consumed"], 5001)

    def test_down_and_unknown_are_not_deaf(self):
        results = check([tick(alive=False, pending=3)] * 3 + [tick(pending=None)] * 3)
        self.assertEqual(verdicts(results), ["down"] * 3 + ["unknown"] * 3)
        self.assertFalse(any(result["restart"] for result in results))

    def test_invalid_observations_and_settings_are_rejected(self):
        for bad in ({"alive": 1, "pending": 0}, {"alive": True, "pending": -1}, {"alive": True, "pending": True},
                    {"alive": True, "pending": 0, "consumed": [1.5]}, {"alive": True, "pending": 0, "extra": 1},
                    {"alive": True, "pending": 0, "consumed": "12"}, ["alive"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check([bad])
        for settings in ({"deaf_ticks": 0}, {"max_jump": 0}, {"last_consumed": -1}, {"deaf_ticks": True}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                check([tick()], **settings)


if __name__ == "__main__":
    unittest.main()
