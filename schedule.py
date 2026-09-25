"""Calendar replay and run-evidence audit for launchd-style jobs. Pure functions, no I/O.

`due_slots` answers "which calendar slots should run now?" on a machine where the scheduler may
not fire them itself: launchd skips calendar jobs when nobody is logged in. `audit` answers "is
this job actually running?" from evidence of work, not from the scheduler's own status.

Nothing here reads plists, logs or the clock, and nothing starts a process. Callers pass parsed
schedule specs, file facts and the current time, which keeps every rule testable with synthetic
jobs.
"""
from datetime import date, datetime, time, timedelta

from reliability import seconds

CATCH_UP_WINDOW = 7200
SLOT_KEY_FORMAT = "%Y-%m-%dT%H:%M"
INTERVAL_STALE_FACTOR = 3
CALENDAR_STALE_FACTOR = 1.5
EVIDENCE_KINDS = ("fire", "artifact", "log")
_FIELDS = {"Minute": (0, 59), "Hour": (0, 23), "Day": (1, 31), "Weekday": (0, 7), "Month": (1, 12)}


def _entries(spec):
    """Validate a StartCalendarInterval value: one dict or a list of dicts."""
    entries = spec if isinstance(spec, list) else [spec]
    if not entries:
        raise ValueError("a calendar spec needs at least one entry")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each calendar entry must be a dict")
        unknown = set(entry) - set(_FIELDS)
        if unknown:
            raise ValueError(f"unknown calendar keys: {sorted(unknown)}")
        for key, value in entry.items():
            low, high = _FIELDS[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} must be an integer from {low} to {high}")
        if "Minute" not in entry:
            raise ValueError("Minute is required: launchd would run this entry every minute, "
                             "and per-minute schedules are not replayed")
        if "Day" in entry and "Weekday" in entry:
            raise ValueError("Day and Weekday together are ambiguous (cron treats the pair as "
                             "either-or); split them into two entries")
    return entries


def _matches_day(entry, day):
    if "Month" in entry and entry["Month"] != day.month:
        return False
    if "Day" in entry and entry["Day"] != day.day:
        return False
    # launchd counts Sunday as 0 and also accepts 7 for Sunday.
    return "Weekday" not in entry or entry["Weekday"] % 7 == day.isoweekday() % 7


def _slots_on(entries, day):
    for entry in entries:
        if _matches_day(entry, day):
            hours = [entry["Hour"]] if "Hour" in entry else range(24)
            for hour in hours:
                yield datetime.combine(day, time(hour, entry["Minute"]))


def due_slots(spec, now, fired=(), window=CATCH_UP_WINDOW):
    """Return the keys of calendar slots that have passed within `window` seconds and not fired.

    `now` is a naive local datetime. A slot is due when it is at or before `now` and less than
    `window` seconds old, so a slot missed while the machine was down still runs once on
    recovery. Keys look like 2026-01-04T06:00 and sort in time order; the caller records each key
    it fires, and `fired` is that record, so each slot runs at most once. When several slots of one
    job are due together, run the job once and record every returned key; launchd coalesces
    missed intervals the same way after sleep.
    """
    entries = _entries(spec)
    if not isinstance(now, datetime) or now.tzinfo is not None:
        raise ValueError("now must be a naive local datetime")
    seconds(window)
    if window == 0:
        raise ValueError("window must be positive")
    fired = set(fired)
    earliest = now - timedelta(seconds=window)
    due = set()
    day = earliest.date()
    while day <= now.date():
        for slot in _slots_on(entries, day):
            key = slot.strftime(SLOT_KEY_FORMAT)
            if earliest < slot <= now and key not in fired:
                due.add(key)
        day += timedelta(days=1)
    return sorted(due)


def calendar_period(spec):
    """Longest gap, in seconds, between consecutive runs of a calendar spec.

    Computed by listing every slot over eight reference years, so uneven schedules get their real
    worst case: runs at 06:00 and 07:00 leave a 23-hour gap, not 12. Eight years include two leap
    days, which covers a spec that only matches 29 February.
    """
    entries = _entries(spec)
    start = date(2001, 1, 1)
    slots = sorted({slot for offset in range(8 * 366)
                    for slot in _slots_on(entries, start + timedelta(days=offset))})
    if len(slots) < 2:
        raise ValueError("the spec never runs, or runs too rarely to measure")
    return max((b - a).total_seconds() for a, b in zip(slots, slots[1:]))


def _last_run(evidence):
    """Newest usable evidence as (time, kind). A zero-byte scheduler log is not a run."""
    best = (None, "none")
    rank = {kind: index for index, kind in enumerate(reversed(EVIDENCE_KINDS))}
    for item in evidence:
        if not isinstance(item, dict) or item.get("kind") not in EVIDENCE_KINDS:
            raise ValueError(f"evidence kind must be one of {EVIDENCE_KINDS}")
        at = seconds(item.get("at"))
        if item["kind"] == "log":
            size = item.get("size")
            if type(size) is not int or size < 0:
                raise ValueError("log evidence needs a byte size")
            if size == 0:
                continue
        if best[0] is None or (at, rank[item["kind"]]) > (best[0], rank[best[1]]):
            best = (at, item["kind"])
    return best


def _check_record(record):
    if not isinstance(record, dict) or not isinstance(record.get("name"), str) or not record["name"]:
        raise ValueError("each job record needs a name")
    if record.get("kind") not in ("interval", "calendar"):
        raise ValueError("kind must be interval or calendar")
    seconds(record.get("period"))
    if record["period"] == 0:
        raise ValueError("period must be positive")
    if record.get("installed_at") is not None:
        seconds(record["installed_at"])
    if type(record.get("loaded", False)) is not bool:
        raise ValueError("loaded must be boolean")
    exit_code = record.get("last_exit")
    if exit_code is not None and type(exit_code) is not int:
        raise ValueError("last_exit must be an integer or None")
    if any(type(code) is not int for code in record.get("designed_exits", ())):
        raise ValueError("designed exits must be integers")


def audit(job_records, now):
    """Return one verdict per job, in input order, each naming the evidence it rests on.

    A record has `name`, `kind` ("interval" or "calendar"), `period` in seconds (for a calendar job,
    `calendar_period(spec)`), and optionally `installed_at` (when the job definition last changed),
    `evidence` (dicts with `kind` "fire", "artifact" or "log", `at`, and `size` for logs),
    `loaded` (the scheduler reports the job loaded), `last_exit` and `designed_exits`.

    The newest usable item wins. Evidence kinds, strongest first (strength breaks a tie between
    equally recent items): a dispatcher "fire" record; an "artifact" the job writes on
    purpose, valid at any size; a scheduler-managed "log", valid only when it is not empty. A
    scheduler's loaded state is never counted as a run.

    Verdicts: OK; NOT-YET-DUE (changed less than one period ago, so it cannot be late yet);
    STALE (evidence older than 3 periods for an interval job, 1.5 for a calendar job); NO-EVIDENCE;
    UNKNOWN (an interval job the scheduler reports loaded, with no run evidence); FAILED (a
    non-zero last exit that is not declared in `designed_exits`); UNKNOWN-CLOCK (a timestamp
    after `now`).
    """
    seconds(now)
    results, names = [], set()
    for record in job_records:
        _check_record(record)
        name, period = record["name"], record["period"]
        if name in names:
            raise ValueError(f"duplicate job name: {name}")
        names.add(name)
        last, source = _last_run(record.get("evidence", ()))
        installed_at = record.get("installed_at")
        exit_code = record.get("last_exit")
        factor = INTERVAL_STALE_FACTOR if record["kind"] == "interval" else CALENDAR_STALE_FACTOR
        age = None if last is None else now - last
        if (last is not None and last > now) or (installed_at is not None and installed_at > now):
            verdict, source, age = "UNKNOWN-CLOCK", "none", None
        elif exit_code not in (None, 0) and exit_code not in record.get("designed_exits", ()):
            verdict, source = "FAILED", "exit-status"
        elif age is not None and age <= period * factor:
            verdict = "OK"
        elif installed_at is not None and now - installed_at < period:
            verdict = "NOT-YET-DUE"
        elif last is not None:
            verdict = "STALE"
        elif record["kind"] == "interval" and record.get("loaded", False):
            verdict, source = "UNKNOWN", "scheduler-state"
        else:
            verdict = "NO-EVIDENCE"
        results.append({"name": name, "verdict": verdict, "evidence": source,
                        "last_run": last, "age": age})
    return results

