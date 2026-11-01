from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from stores import freshest_store, scheduler_health

NOW = 1_000_000
MINUTE = 60
JOB = "synthetic-rearm"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name) / "state-legacy.db"
        self.new = Path(self.temp.name) / "state.db"
        self.paths = [self.new, self.old]

    def store(self, path, last_run, status="ACTIVE"):
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, last_run_at REAL)")
            conn.executemany("INSERT INTO jobs VALUES (?,?,?)",
                             [(JOB, status, last_run), ("other", "ACTIVE", last_run)])
            conn.commit()

    def health(self):
        return scheduler_health(self.paths, JOB, NOW, max_age=10 * MINUTE)

    def test_a_frozen_old_store_alone_reads_as_stale(self):
        self.store(self.old, NOW - 12 * 60 * MINUTE)
        row = self.health()
        self.assertEqual((row["verdict"], row["store"]), ("stale", str(self.old)))
        self.assertEqual(row["skipped"], [{"store": str(self.new), "reason": "missing"}])

    def test_the_live_store_wins_and_the_verdict_names_it(self):
        self.store(self.old, NOW - 12 * 60 * MINUTE)
        self.store(self.new, NOW - MINUTE)
        row = self.health()
        self.assertEqual((row["verdict"], row["store"], row["last_run_at"]), ("ok", str(self.new), NOW - MINUTE))

    def test_order_of_the_known_stores_does_not_matter(self):
        self.store(self.old, NOW - 12 * 60 * MINUTE)
        self.store(self.new, NOW - MINUTE)
        self.paths.reverse()
        self.assertEqual(self.health()["store"], str(self.new))

    def test_a_stall_in_every_store_still_reads_as_stale(self):
        self.store(self.old, NOW - 12 * 60 * MINUTE)
        self.store(self.new, NOW - 25 * MINUTE)
        self.assertEqual(self.health()["verdict"], "stale")

    def test_a_pause_in_the_live_store_is_not_hidden_by_an_old_active_row(self):
        self.store(self.old, NOW - 12 * 60 * MINUTE)
        self.store(self.new, NOW - MINUTE, status="PAUSED")
        row = self.health()
        self.assertEqual((row["verdict"], row["status"], row["store"]), ("paused", "PAUSED", str(self.new)))

    def test_no_readable_store_is_unknown_not_healthy(self):
        self.new.write_text("not a database")
        row = self.health()
        self.assertEqual(row["verdict"], "unknown")
        self.assertEqual([item["reason"] for item in row["skipped"]], ["unreadable: DatabaseError", "missing"])

    def test_a_job_missing_from_the_live_store_is_reported(self):
        self.store(self.new, NOW - MINUTE)
        self.assertEqual(scheduler_health(self.paths, "absent-job", NOW, 600)["verdict"], "missing_job")

    def test_reading_never_modifies_a_store(self):
        self.store(self.old, NOW - 12 * 60 * MINUTE)
        self.store(self.new, NOW - MINUTE)
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in Path(self.temp.name).iterdir()}
        self.health()
        freshest_store(self.paths)
        after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in Path(self.temp.name).iterdir()}
        self.assertEqual(before, after)

    def test_invalid_arguments_are_rejected(self):
        for kwargs in ({"job": ""}, {"now": -1}, {"max_age": 0}):
            args = dict(paths=self.paths, job=JOB, now=NOW, max_age=600)
            args.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                scheduler_health(**args)


if __name__ == "__main__":
    unittest.main()
