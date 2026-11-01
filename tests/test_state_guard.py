import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import state_guard
from state_guard import RealStateWrite, default_state_dir, fingerprint, guard_write, record_failure

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tests" / "run-isolated.sh"


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.real = Path(self.temp.name) / "real-home"
        self.scratch = Path(self.temp.name) / "scratch-home"
        self.real.mkdir()
        self.scratch.mkdir()

    def test_a_test_that_kept_the_real_home_is_refused_and_writes_nothing(self):
        # The leak: HOME still pointed at the operator's home, so the default state path was real.
        with patch.dict(os.environ, {"HOME": str(self.real)}):
            with self.assertRaises(RealStateWrite):
                record_failure({"at": 1, "fault": "synthetic"}, real_home=self.real)
        self.assertEqual(fingerprint(self.real), "empty")

    def test_a_temporary_home_is_allowed(self):
        with patch.dict(os.environ, {"HOME": str(self.scratch)}):
            target = record_failure({"at": 1, "fault": "synthetic"}, real_home=self.real)
        self.assertEqual(target, (default_state_dir(self.scratch) / "failure-history.jsonl").resolve())
        self.assertEqual(json.loads(target.read_text()), {"at": 1, "fault": "synthetic"})

    def test_the_real_home_comes_from_the_account_not_from_HOME(self):
        with patch.dict(os.environ, {"HOME": str(self.scratch)}):
            self.assertEqual(state_guard.account_home(), Path(state_guard.pwd.getpwuid(os.getuid()).pw_dir).resolve())
            self.assertTrue(state_guard.under_test())

    def test_outside_a_test_run_the_guard_does_not_interfere(self):
        path = self.real / ".local/state/agent-reliability/failure-history.jsonl"
        self.assertEqual(guard_write(path, real_home=self.real, testing=False), path.resolve())
        with self.assertRaises(RealStateWrite):
            guard_write(path, real_home=self.real, testing=True)
        with self.assertRaises(RealStateWrite):
            guard_write(self.real, real_home=self.real, testing=True)

    def test_a_symlink_into_the_real_home_is_still_refused(self):
        link = self.scratch / "state-link"
        link.symlink_to(self.real, target_is_directory=True)
        with self.assertRaises(RealStateWrite):
            record_failure({"at": 1}, state_dir=link, real_home=self.real, testing=True)

    def test_fingerprint_changes_when_a_file_is_written(self):
        state = self.real / ".local/state/agent-reliability"
        self.assertEqual(fingerprint(state), "absent")
        state.mkdir(parents=True)
        self.assertEqual(fingerprint(state), "empty")
        (state / "history.jsonl").write_text("{}\n")
        first = fingerprint(state)
        self.assertIn("history.jsonl\t3\t", first)
        self.assertEqual(first, fingerprint(state))


class RunnerTests(unittest.TestCase):
    """Runs tests/run-isolated.sh on throwaway suites with a stand-in for the real home."""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.real = Path(self.temp.name) / "real-home"
        self.real_state = self.real / ".local/state/agent-reliability"
        self.real_state.mkdir(parents=True)
        (self.real_state / "failure-history.jsonl").write_text('{"at": 0}\n')
        self.suite = Path(self.temp.name) / "suite"
        self.suite.mkdir()

    def run_suite(self, body):
        (self.suite / "test_fixture.py").write_text(body)
        env = {"HOME": str(self.real), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "FIXTURE_TOKEN": "synthetic-inherited-value"}
        return subprocess.run(["bash", str(RUNNER), str(self.suite)], cwd=ROOT, env=env,
                              text=True, capture_output=True, timeout=60)

    def test_suites_get_a_fresh_home_and_no_inherited_variables(self):
        result = self.run_suite(
            "import os, pathlib, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_env(self):\n"
            f"        self.assertNotEqual(os.environ['HOME'], {str(self.real)!r})\n"
            "        self.assertNotIn('FIXTURE_TOKEN', os.environ)\n"
            "        self.assertEqual(list(pathlib.Path.home().iterdir()), [])\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_write_that_bypasses_the_guard_still_fails_the_run(self):
        leak = self.real_state / "failure-history.jsonl"
        result = self.run_suite(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_leak(self):\n"
            f"        open({str(leak)!r}, 'a').write('{{\"fixture\": 1}}\\n')\n")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("real state directory changed", result.stderr)

    def test_a_failing_suite_keeps_its_exit_code(self):
        result = self.run_suite("import unittest\nclass T(unittest.TestCase):\n"
                                "    def test_fail(self):\n        self.fail('synthetic')\n")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
