# From a private runtime to a portable example

The private system uses shell scripts, scheduled jobs, and machine-specific health probes. Those integrations are deliberately absent here. The public example takes structured observations so a reader can inspect the policy without installing jobs or connecting accounts. Lessons from running the private system are in [operations-notes.md](operations-notes.md).

The earlier implementation supplied the core alert policy: boot suppression, host-health suppression, spaced confirmations, deduplication, and escalation. Its job audit supplied the distinction between process liveness and evidence of successful work. This edition implements those ideas with explicit clocks and transactional state, rather than presenting the operational scripts as a reusable library.

## Alert lifecycle

```mermaid
flowchart LR
    A[Observation] --> B{Boot or host suppression?}
    B -->|Yes| C[Record suppression]
    B -->|No| D{Enough spaced evidence?}
    D -->|No| C
    D -->|Yes| E{Pending attempt or recent delivery?}
    E -->|Yes| C
    E -->|No, or yellow-to-red after a delivery| F[Pending outbox entry]
    F --> G{Caller acknowledgement}
    G -->|Success| H[Start dedupe window]
    G -->|Failure| I[Allow a later observation to retry]
```

Each observation, its state change and any outbox entry are committed in one SQLite transaction. The transport is outside that transaction, so this design does not promise exactly-once notification delivery.

Event IDs make exact retries idempotent. Reusing an ID for a different payload fails. The stored policy must match on reopen so a process cannot silently reinterpret an existing confirmation history with new thresholds.

A clear observation resets the incident and cancels pending attempts. Cancellation cannot retract a message already sent by an external worker. A real adapter needs its own claim, cancellation and reconciliation rules.

## Deliberate trade-offs

- Time is supplied by the caller for reproducible tests. Production deployments need a defined clock source and skew policy.
- A pending attempt blocks new attempts, including escalation. This avoids duplicate ownership but requires operational resolution of abandoned attempts. The demo never guesses whether a timed-out delivery actually happened.
- The policy uses one incident class at a time. Confirmation is about a persistent problem in that class, rather than independent statistical evidence.
- The outbox contains metadata only. A real transport should keep sensitive message bodies out of general logs and apply retention rules.
- `job_health` assumes a fixed expected period plus grace. [`schedule.py`](../schedule.py) adds the calendar side as pure functions: `due_slots` replays launchd-style calendar specs inside a two-hour catch-up window, at most once per slot, and `audit` grades run evidence per job, ignoring zero-byte logs and giving a newly installed job one period before it can be late. It does not read plists or logs, start jobs, model daylight-saving transitions, replay per-minute schedules, or know about holidays and other planned skips.

## Delivery reconciliation

[`delivery.py`](../delivery.py) keeps four tables for one poller: the seen set, the delivery ledger, a re-delivery queue and the time of the last successful poll.

```mermaid
flowchart LR
    A[List the inbox] -->|fails| B[Change nothing]
    A -->|ok| C[Unseen or queued message]
    C --> D{Body fetched?}
    D -->|No| E[Leave unseen]
    D -->|Yes| F{Sender returned True?}
    F -->|No| E
    F -->|Yes| G[Seen + ledger + dequeue in one transaction]
    H[Audit: inbox vs ledger] -->|missing| I[Queue it, drop it from seen]
    I -->|still missing after grace| J[Escalate once]
```

- `poll` always writes the seen set and the ledger together, yet they stay separate tables. The audit compares the inbox with the ledger, never with the seen set, so it also catches ids an earlier poller marked seen too soon. A re-queued id is removed from the seen set.
- A message is expected in the ledger only if it arrived at least `settle` seconds before the last successful poll. A message that arrived after the last real poll is not missing yet.
- A poll whose list call succeeded refreshes the marker even when a fetch failed. The failed message stays unseen and inside the lookback window. If it keeps failing, the audit queues it, and the grace period decides when a person hears about it.
- The poll window is at least `lookback` seconds and reaches back to the last successful poll after an outage. Queued messages are retried even when they are older than that window.
- Escalated messages stay queued, so polls keep retrying them. Escalation is returned once per message, and the module sends nothing.

## Listener receiving check

[`listener.py`](../listener.py) is a pure function over ticks. Each tick has a process state, the provider's count of waiting updates and the update ids the listener recorded since the previous tick.

- A tick that consumed updates is never deaf, even with updates waiting, because a busy chat can always have one waiting at the moment of the check.
- Only consecutive deaf ticks count. Any other verdict resets the count, including `unknown` when the provider could not be asked. This is the conservative choice: a flaky provider delays a restart rather than causing one.
- A gap or an inconclusive id sequence is reported and never restarts anything. Restarting cannot recover updates another consumer already took.
- `down` is left to the process check. This module adds the receiving check on top of it and does not replace it.

## What changed for publication

The release adds transactional decisions, input validation, explicit delivery acknowledgement and concurrency tests. The fixture timeline is invented and does not reproduce a private incident. The release does not claim these new components are deployed in the original runtime.

`schedule.py` is a new implementation of the private dispatcher and job audit rules; the private scripts read plists, logs and `launchctl` directly and are not published. It differs from them on purpose in four places: it rejects entries without `Minute` or with both `Day` and `Weekday` instead of guessing, its catch-up window can cross midnight, a calendar job's period is its longest real gap rather than a rounded estimate, and an interval job whose only signal is a loaded status is `UNKNOWN` rather than `OK`. `delivery.py` and `listener.py` are new implementations of two checks the private system gained in late September 2026; the private scripts read a real inbox, a delivery ledger file, the provider's API and the listener's log, and are not published. They differ on purpose in four places: escalated messages stay queued here (the private audit drops them from its queue), a tick that consumed updates is never deaf here (the private watchdog looks only at the waiting count), the contiguity result here sits in the same verdict as the deaf check instead of a separate log, and an `unknown` tick resets the deaf count here, while the private watchdog leaves its count unchanged when the provider cannot be asked. Their fixtures are synthetic and reproduce the failure shapes, not the incidents' data; their tests show what the public modules do, not what the private scripts do.

`guards/notify-guard.sh` is adapted from the private guard: its patterns are the same, the first lines are read with bash builtins instead of a pipe, and its test fixtures are neutral sentences.
