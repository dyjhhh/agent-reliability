"""Is a chat bot's listener receiving, or only running? A pure function over caller observations.

A process check answers "is the listener running?". It cannot tell a quiet chat from a listener
that stopped consuming: a listener can stop polling after repeated errors while its process stays
up. Two signals the process check does not use:

- the provider's count of updates waiting unconsumed. A live poller takes an update within
  seconds, so updates still waiting on several consecutive ticks mean nobody is consuming them;
- the update ids the listener recorded. Where a provider numbers a bot's updates one by one, the
  next id after the last consumed one should be that id plus one. A contiguous run shows that a
  long silence was real; a jump shows updates that never reached this listener, such as updates
  taken by a second consumer.

One verdict per tick:

    healthy       consumed updates this tick, ids contiguous with the last consumed id
    quiet         alive, nothing waiting, nothing consumed
    deaf          alive, updates waiting, nothing consumed this tick
    gap           consumed updates, but ids were skipped; `missing` counts them
    inconclusive  an id went backwards or jumped more than `max_jump` (the provider renumbered);
                  the check re-arms at the new id
    down          the process is not running; restarting it is the process check's job
    unknown       the waiting count could not be read, so there is no verdict

`restart` is True on the tick that completes `deaf_ticks` consecutive deaf ticks. Any other
verdict resets the count, and so does the restart itself. A gap is a report, never a restart.
Nothing here reads a provider, a log or the process table, and nothing starts or stops a process.
"""

VERDICTS = ("healthy", "quiet", "deaf", "gap", "inconclusive", "down", "unknown")


def _count(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _observation(item):
    if not isinstance(item, dict) or set(item) - {"alive", "pending", "consumed"}:
        raise ValueError("an observation is a dict with alive, pending and consumed")
    if type(item.get("alive")) is not bool:
        raise ValueError("alive must be boolean")
    pending = item.get("pending")
    if pending is not None:
        _count(pending, "pending")
    consumed = item.get("consumed", ())
    if not isinstance(consumed, (list, tuple)):
        raise ValueError("consumed must be a list of update ids")
    return item["alive"], pending, [_count(update_id, "an update id") for update_id in consumed]


def check(observations, deaf_ticks=3, last_consumed=None, max_jump=1000):
    """Return one result per observation, in order.

    Each observation has `alive` (the process check's answer), `pending` (updates waiting at the
    provider, or None when the provider could not be asked) and `consumed` (the update ids the
    listener recorded since the previous tick, in order). `last_consumed` arms the contiguity check
    with the newest id seen before the first tick; without it, the first consumed id arms it.

    Each result has `verdict`, `restart`, `deaf_streak`, `missing` (skipped ids this tick) and
    `last_consumed`.
    """
    _count(deaf_ticks, "deaf_ticks")
    _count(max_jump, "max_jump")
    if deaf_ticks == 0 or max_jump == 0:
        raise ValueError("deaf_ticks and max_jump must be positive")
    if last_consumed is not None:
        _count(last_consumed, "last_consumed")
    results, streak, last = [], 0, last_consumed
    for item in observations:
        alive, pending, consumed = _observation(item)
        missing, renumbered = 0, False
        for update_id in consumed:
            if last is not None:
                step = update_id - last
                if step < 1 or step > max_jump:
                    renumbered = True
                else:
                    missing += step - 1
            last = update_id
        if not alive:
            verdict = "down"
        elif consumed:
            verdict = "inconclusive" if renumbered else "gap" if missing else "healthy"
        elif pending is None:
            verdict = "unknown"
        else:
            verdict = "deaf" if pending > 0 else "quiet"
        streak = streak + 1 if verdict == "deaf" else 0
        restart = streak >= deaf_ticks
        results.append({"verdict": verdict, "restart": restart, "deaf_streak": streak,
                        "missing": missing, "last_consumed": last})
        if restart:
            streak = 0
    return results
