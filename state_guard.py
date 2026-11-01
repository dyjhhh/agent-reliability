"""Keep test runs out of the operator's real state directory.

On the private host, a test run used the operator's real HOME. Its fixtures wrote a failure and a
recovery a millisecond apart into the production failure history, and the health check, reading that
history, sent three "recovered" notices for outages that never happened.

Two independent layers, so that either one alone stops the leak:

- `guard_write` refuses a write inside the account's real home while a test framework is loaded. The
  real home comes from the password database, not from `$HOME`, so a test that forgot to set a
  temporary `$HOME` is caught instead of trusted.
- `tests/run-isolated.sh` runs the suites with a fresh temporary `HOME` and `env -i` (no inherited
  variables), and compares a fingerprint of the real state directory before and after the run. A
  test that writes there by any route, including a plain `open()` that bypasses the guard, fails the
  run.

`record_failure` is a small state writer that uses the guard, standing in for any code that derives
its state path from the home directory. Nothing here deletes files.
"""
import json
import os
import pwd
import sys
from pathlib import Path

APP = "agent-reliability"
TEST_FRAMEWORKS = ("unittest", "pytest")


class RealStateWrite(RuntimeError):
    """A test tried to write inside the operator's real home."""


def account_home():
    """The home directory the account really has, whatever `$HOME` says."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def under_test():
    return any(name in sys.modules for name in TEST_FRAMEWORKS)


def default_state_dir(home=None):
    return Path(home if home is not None else Path.home()) / ".local/state" / APP


def guard_write(path, real_home=None, testing=None):
    """Return the resolved path, or raise RealStateWrite when a test targets the real home."""
    target = Path(path).resolve()
    real = Path(real_home).resolve() if real_home is not None else account_home()
    testing = under_test() if testing is None else testing
    if testing and (target == real or real in target.parents):
        raise RealStateWrite(f"refusing to write {target}: it is inside the real home {real} during a "
                             f"test run; run the suite through tests/run-isolated.sh")
    return target


def record_failure(entry, state_dir=None, real_home=None, testing=None):
    """Append one JSON line to failure-history.jsonl in the state directory."""
    if not isinstance(entry, dict):
        raise ValueError("a failure entry is a dict")
    directory = Path(state_dir) if state_dir is not None else default_state_dir()
    target = guard_write(directory / "failure-history.jsonl", real_home, testing)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return target


def fingerprint(directory):
    """Sorted (relative path, size, mtime_ns) lines for every file, or "absent". Read-only."""
    root = Path(directory)
    if not root.exists():
        return "absent"
    lines = []
    for current, _dirs, files in os.walk(root):
        for name in files:
            item = Path(current) / name
            info = item.lstat()
            lines.append(f"{item.relative_to(root)}\t{info.st_size}\t{info.st_mtime_ns}")
    return "\n".join(sorted(lines)) or "empty"


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "fingerprint":
        sys.exit("usage: state_guard.py fingerprint DIRECTORY")
    print(fingerprint(sys.argv[2]))
