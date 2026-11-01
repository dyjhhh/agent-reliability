"""Expired test evidence is a reminder, not an outage. A pure function over a receipt and saved state.

An alert channel is accepted only when a person or a device has observed it working end to end, for
example a phone alarm that actually sounded. That observation is valid for a set number of days. On
the private host, an expired observation was reported through the same path as a broken channel, so
for about 32 hours the operator was paged about an outage that did not exist: every channel was
working, and only the proof of it had aged out.

Two separate outputs:

- `page` lists items whose latest recorded test explicitly FAILED. These are incidents and go to the
  alert gate on every evaluation. A failure never ages into a reminder; only a later pass clears it.
- the reminder channel sends one `retest_due` notice per newly due set of items and one
  `retest_cleared` notice when every item it announced passes again. An item is announced at most
  once per cycle, so an item that flaps between due and passing cannot cause a second reminder.

An unreadable receipt is unknown, not "never tested": it pages nothing, reminds nothing and leaves
the saved state as it was. Nothing here reads files or sends anything.
"""
import re

from reliability import seconds

RESULTS = ("pass", "failed", "expired", "never_observed", "not_passed")
DUE = frozenset({"expired", "never_observed", "not_passed"})
FAILED_STATUS = re.compile(r".*FAIL.*|NOT_(?:HEARD|RECEIVED|DELIVERED|RUNG)|NO_(?:SOUND|RECEIPT|DELIVERY)")


def _result(item, window, now):
    if not isinstance(item, dict):
        return "never_observed"
    status = item.get("status")
    status = status.strip().upper() if isinstance(status, str) else ""
    if FAILED_STATUS.fullmatch(status):
        return "failed"
    observed = item.get("observed_at")
    if observed is None:
        return "never_observed"
    age = now - seconds(observed)
    if age < 0:
        return "not_passed"  # a future observation is not evidence
    if age > window:
        return "expired"
    return "pass" if status == "PASS" and item.get("evidence") else "not_passed"


def classify(receipt, now, validity):
    """Map each item in `validity` (name -> seconds an observation stays valid) to a result.

    `receipt` maps names to {"status", "observed_at", "evidence"}; None means the receipt could not
    be read, and the return value is then None.
    """
    seconds(now)
    if not isinstance(validity, dict) or not validity:
        raise ValueError("validity maps each item to a window in seconds")
    for window in validity.values():
        if seconds(window) == 0:
            raise ValueError("validity windows must be positive")
    if receipt is None:
        return None
    if not isinstance(receipt, dict):
        raise ValueError("a receipt is a dict, or None when it could not be read")
    return {name: _result(receipt.get(name), window, now) for name, window in sorted(validity.items())}


def decide(results, state):
    """Return {"page", "notices", "state", "decision"}. `state` is {"announced": [...]} or {}."""
    announced = set(state.get("announced", ())) if isinstance(state, dict) else set()
    if results is None:
        return {"page": [], "notices": [], "state": {"announced": sorted(announced)}, "decision": "unknown"}
    unknown = set(results.values()) - set(RESULTS)
    if unknown:
        raise ValueError(f"unknown results: {sorted(unknown)}")
    page = sorted(name for name, result in results.items() if result == "failed")
    due = {name for name, result in results.items() if result in DUE}
    passed = {name for name, result in results.items() if result == "pass"}
    announced &= set(results)
    notices, decision = [], "no_change"
    newly = due - announced
    if newly:
        notices.append({"kind": "retest_due", "items": sorted(due), "new": sorted(newly),
                        "text": "\U0001F7E1 Retest due (reminder, not an outage): " + ", ".join(
                            f"{name} {results[name].replace('_', ' ')}" for name in sorted(due))})
        announced |= due
        decision = "retest_due"
    elif announced and announced <= passed:
        notices.append({"kind": "retest_cleared", "items": sorted(announced),
                        "text": "\U0001F7E2 Retest recorded: " + ", ".join(sorted(announced)) + " pass again"})
        announced = set()
        decision = "retest_cleared"
    if page:
        decision = "page" if decision == "no_change" else decision + "+page"
    return {"page": page, "notices": notices, "state": {"announced": sorted(announced)}, "decision": decision}
