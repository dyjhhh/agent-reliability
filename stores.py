"""Read a scheduler's state from whichever of its known stores is live. Read-only.

On the private host, a desktop agent app ran scheduled jobs and recorded each run in a SQLite file.
An automatic app update on 2026-10-30 moved that record to a new database file. The old file
stopped changing at its last run, and the health check, which still read the old file, reported the
scheduler as stalled while the jobs kept running and writing to the new one.

`freshest_store` opens every known store read-only and picks the one with the newest recorded run. A
store only records runs that happened, so picking the newest cannot invent freshness: a stall that
shows in every store is still a stall. `scheduler_health` reads the job's status and last run from
that one store and returns the store it used, so a reader of the verdict can see which file it came
from. A paused job in the live store is not hidden by an old active row in a frozen one.

The table layout is a small synthetic one: `jobs(id, status, last_run_at)`, times in seconds.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

from reliability import seconds


def _read_only(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.execute("PRAGMA query_only=ON")
    return closing(conn)


def freshest_store(paths):
    """Return {"store", "latest", "skipped"} for the store with the newest run, or store None."""
    best, best_at, skipped = None, None, []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            skipped.append({"store": str(path), "reason": "missing"})
            continue
        try:
            with _read_only(path) as conn:
                latest = conn.execute("SELECT MAX(last_run_at) FROM jobs").fetchone()[0]
        except sqlite3.Error as error:
            skipped.append({"store": str(path), "reason": "unreadable: " + type(error).__name__})
            continue
        if isinstance(latest, bool) or not isinstance(latest, (int, float)):
            skipped.append({"store": str(path), "reason": "no runs"})
            continue
        if best_at is None or latest > best_at:
            best, best_at = path, latest
    return {"store": str(best) if best else None, "latest": best_at, "skipped": skipped}


def scheduler_health(paths, job, now, max_age):
    """Verdict for one job: ok, stale, paused, missing_job or unknown (no readable store)."""
    seconds(now)
    if seconds(max_age) == 0:
        raise ValueError("max_age must be positive")
    if not isinstance(job, str) or not job:
        raise ValueError("job must be a nonempty string")
    chosen = freshest_store(paths)
    result = {"job": job, "store": chosen["store"], "skipped": chosen["skipped"], "last_run_at": None}
    if chosen["store"] is None:
        return dict(result, verdict="unknown")
    with _read_only(chosen["store"]) as conn:
        row = conn.execute("SELECT status, last_run_at FROM jobs WHERE id=?", (job,)).fetchone()
    if row is None:
        return dict(result, verdict="missing_job")
    status, last_run = row
    result["last_run_at"] = last_run
    if status != "ACTIVE":
        return dict(result, verdict="paused", status=status)
    if not isinstance(last_run, (int, float)) or now - last_run > max_age:
        return dict(result, verdict="stale")
    return dict(result, verdict="ok")
