"""A refused run is not an attempt. Run accounting for a capped batch scorer. Pure functions.

On the private host, a scheduled job sends a queued batch of items to a hosted model for scoring. It
has a daily cap on items and a limit of two attempts per input window. On 2026-10-28 the provider
refused the run with a spend-limit message: exit status 1, nothing scored, no tokens used. The ledger
recorded it like any other run, so the batch was charged against the day's cap and the run counted
as an attempt, and the rest of that day's work did not run.

`classify_run` tells a provider's limit refusal apart from a real failure. Only a run that scored
nothing, exited non-zero and printed a spend, usage or credit limit message is a refusal. `record`
writes refusals with `charged` False, so `budget` counts them against neither the daily cap nor the
attempt limit. `settle` keeps the queue and the watermark on a refusal, so the next tick retries the
same window. Nothing here runs a model or reads a file.
"""
from dataclasses import dataclass
import re

OUTCOMES = ("complete", "partial", "limit_refused", "failed")
LIMIT_REFUSAL = re.compile(r"\b(?:hit|reached|exceeded)\s+(?:your\s+|the\s+)?(?:[a-z]+\s+){0,2}"
                           r"(?:spend|spending|usage|credit)\s+limit\b"
                           r"|\b(?:spend|spending|usage|credit)\s+limit\s+(?:reached|exceeded)\b", re.I)


def _count(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


@dataclass(frozen=True)
class BudgetPolicy:
    daily_cap: int = 25
    max_attempts: int = 2

    def __post_init__(self):
        _count(self.daily_cap, "daily_cap")
        _count(self.max_attempts, "max_attempts")
        if self.max_attempts == 0:
            raise ValueError("max_attempts must be positive")


def classify_run(exit_code, output, queued, scored, complete=False):
    """Outcome of one run. `complete` is the run's own receipt saying it finished its batch."""
    if type(exit_code) is not int:
        raise ValueError("exit_code must be an integer")
    if not isinstance(output, str):
        raise ValueError("output must be text")
    _count(queued, "queued")
    _count(scored, "scored")
    if scored > queued:
        raise ValueError("scored cannot exceed queued")
    if complete and exit_code == 0 and scored == queued:
        return "complete"
    if scored:
        return "partial"
    if exit_code != 0 and LIMIT_REFUSAL.search(output):
        return "limit_refused"
    return "failed"


def record(ledger, day, window, outcome, queued, scored):
    """Return a new ledger with one entry appended. A refusal is recorded but not charged."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    entry = {"day": day, "window": window, "outcome": outcome, "queued": _count(queued, "queued"),
             "scored": _count(scored, "scored"), "charged": outcome != "limit_refused"}
    return list(ledger) + [entry]


def budget(ledger, day, window, policy=BudgetPolicy()):
    """Items charged today, the remaining cap, charged attempts on this window and whether to run."""
    charged = [entry for entry in ledger if entry.get("charged")]
    used = sum(entry["queued"] for entry in charged if entry.get("day") == day)
    attempts = sum(1 for entry in charged if entry.get("window") == window)
    refusals = sum(1 for entry in ledger if entry.get("outcome") == "limit_refused" and entry.get("day") == day)
    remaining = max(policy.daily_cap - used, 0)
    return {"used": used, "remaining": remaining, "attempts": attempts, "refusals_today": refusals,
            "may_run": remaining > 0 and attempts < policy.max_attempts}


def settle(state, outcome, window_end, scored_ids=()):
    """Next {"watermark", "queue"}. Only a complete run advances the watermark."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    done = set(scored_ids)
    queue = [item for item in state.get("queue", []) if item not in done]
    if outcome == "complete":
        return {"watermark": window_end, "queue": queue}
    if outcome == "partial":
        return {"watermark": state.get("watermark"), "queue": queue}
    return {"watermark": state.get("watermark"), "queue": list(state.get("queue", []))}
