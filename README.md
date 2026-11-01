# Agent Reliability

**Problem:** an agent can be running while its work is failing: missed jobs, lost messages, alerts for outages that never happened.\
**What it shows:** reliability judgment for always-on agents: real incidents turned into small modules with failure-path tests.\
**Run it in about 60 seconds:** clone, then `make test` (Python 3.10+ and bash; no third-party packages; fresh temporary HOME).

[![tests](https://github.com/dyjhhh/agent-reliability/actions/workflows/test.yml/badge.svg)](https://github.com/dyjhhh/agent-reliability/actions/workflows/test.yml)

I built monitoring and recovery tools for a personal agent system after running into missed jobs, noisy alerts, and checks that trusted process status too much.

This public edition isolates ten parts of that work: **deciding when to alert**, **checking evidence that a job actually succeeded**, **replaying calendar jobs the scheduler skipped**, **refusing to send a runner's error text as a report**, **reconciling forwarded messages with the inbox they came from**, **telling a quiet chat listener from one that stopped receiving**, **keeping test runs out of real state**, **treating expired test evidence as a reminder rather than an outage**, **reading a scheduler's state from whichever store is live**, and **not charging a provider's limit refusal against a daily cap**. It runs locally with synthetic observations and sends no messages.

## Try it

To run the demo and each suite separately:

```sh
python3 demo.py
bash tests/run-isolated.sh                 # 87 unittest cases for the eight Python modules, fresh HOME, env -i
bash tests/test_notify_guard.sh            # 10 bash cases for guards/notify-guard.sh
```

`make test` runs both, plus 5 unittest cases for the pattern scanner in `tools/`. The Python suites run only through `tests/run-isolated.sh`, which fails the run if the real state directory changed.

The demo walks through boot suppression, a degraded host, spaced confirmation, a failed delivery, a retry, deduplication, escalation, and recovery. It then polls a synthetic inbox through a fetch timeout and a failed list call, audits it, and runs a listener that stays alive while it stops receiving. Last, it re-reads expired test evidence for 32 simulated hours, reads a scheduler from an old and a new store, and records a spend-limit refusal before a complete run. Its SQLite databases live in a temporary directory and are removed afterward.

## Decisions worth inspecting

| Problem | Implementation | Check |
|---|---|---|
| A transient failure pages the operator | Require observations separated in time; old observations expire | Confirmation spacing and expiry tests |
| A host problem makes every service look broken | Suppress during boot and when the caller reports host degradation | Suppressed checks do not build confirmation history |
| A failed send silently starts the dedupe timer | Store a pending attempt separately; update delivery state only after acknowledgement | Failure permits retry; pending work prevents a second attempt |
| Two workers notice the same failure | Serialize decisions with a SQLite transaction | Concurrent observers create one pending attempt |
| A restart forgets what is already in flight | Persist decisions, policy and the outbox | Reopening the database retains pending work |
| A job is loaded but has never completed | Use the last successful-work timestamp | Missing evidence returns `unknown`, not `healthy` |
| In the private deployment, launchd skipped calendar jobs while nobody was logged in | `schedule.due_slots` replays passed slots inside a two-hour catch-up window, once per slot | Sunday as 0 or 7, hourly, month and day, and midnight-crossing tests |
| A job's log looks fresh after the job stopped | `schedule.audit` uses the newest usable evidence (dispatcher record, job artifact or non-empty log; the stronger kind wins a tie) and ignores zero-byte logs | A synthetic job that genuinely stopped is still reported `STALE` |
| A runner's error text is sent as the report | `guards/notify-guard.sh` checks the first lines for runner and API errors before sending | A real report that quotes "Error:" still passes |
| A checker marks a message seen before fetching it, so one fetch timeout loses the message | `delivery.py` marks a message seen only after its body is fetched and the sender returns `True`, in the transaction that writes the delivery ledger | A fetch timeout leaves the message unseen and the next poll delivers it; `False`, `None`, other values and exceptions from the sender are not deliveries |
| A failed list call reads as an empty inbox and still refreshes the success marker | A list failure changes no state, so the next poll's window reaches back to the last real success | A message that arrived during the outage is delivered by the next poll |
| Every health signal measures a step ("the list call worked"), not whether the message arrived | `DeliveryLedger.audit` compares the inbox with the ledger, re-queues what is missing, and escalates once if it is still undelivered after a grace period | The audit re-queues exactly the message the old ordering lost; nothing escalates inside the grace period and one escalation follows it; re-runs change nothing |
| A listener process stays alive after it stops consuming updates | `listener.check` treats updates waiting unconsumed on three consecutive ticks as deaf and returns a restart decision | An alive-but-deaf listener restarts on the third tick only; a busy listener with updates waiting is not deaf |
| A long silence could be a quiet chat or lost updates | The ids a listener records must continue from the last consumed id plus one; a jump is reported as a gap and never restarts anything | Contiguous ids after fifty quiet ticks; skipped ids counted; renumbered ids reported as inconclusive |
| A test run with the real HOME wrote fixtures into production state, and a health check sent notices about them | `tests/run-isolated.sh` gives every suite a fresh temporary HOME and `env -i`, and fingerprints the real state directory before and after; `state_guard.guard_write` refuses writes inside the account's real home during a test | A fixture that writes past the guard with a plain `open()` fails the run with exit 3; inherited variables do not reach the tests; a symlink into the real home is refused |
| Expired end-to-end test evidence was reported as an outage for 32 hours | `acceptance.decide` sends one yellow retest reminder per newly due set and one green notice when every announced item passes; only an explicit failed test pages | 384 checks of the same expired evidence give one reminder and no page; a failed retest pages every time and never clears; an unreadable receipt changes nothing |
| An app update moved the scheduler's run record to a new database and the health check kept reading the frozen one | `stores.scheduler_health` opens every known store read-only, uses the one with the newest run and names it in the verdict | The live store wins in either order; a stall in every store is still stale; a pause in the live store is not hidden; reading changes no file |
| A spend-limit refusal scored nothing but used the day's cap and an attempt | `budget.classify_run` recognizes the refusal; `record` writes it uncharged; `settle` keeps the queue and watermark | Three refusals and a success charge the batch once; a run that scored anything is partial and charged; a crash is still an attempt |

Start with [`reliability.py`](reliability.py), then read the [tests](tests/test_reliability.py) and [design notes](docs/design.md). [`schedule.py`](schedule.py) and its [tests](tests/test_schedule.py) cover scheduling. [`delivery.py`](delivery.py) and [`listener.py`](listener.py), with the [delivery tests](tests/test_delivery.py) and [listener tests](tests/test_listener.py), cover forwarded messages and chat listeners. [`state_guard.py`](state_guard.py), [`acceptance.py`](acceptance.py), [`stores.py`](stores.py) and [`budget.py`](budget.py), with their [tests](tests/), come from four incidents in late October 2026. [Operations notes](docs/operations-notes.md) record lessons from the private deployment, whose scripts are not published.

## Where the output guard fits

`job_health` and `schedule.audit` ask whether a job left fresh evidence of success. Neither looks at what the job produced. [`guards/notify-guard.sh`](guards/notify-guard.sh) covers one narrow failure in that gap: a runner that hits its turn limit or loses authentication still prints text, and a job that forwards whatever was printed sends the runner's own error message as the report. The guard runs just before sending. It checks the first three lines for runner and API error text, treats empty output as a failure, and lets a real report that quotes "Error:" in its prose through. [`tests/test_notify_guard.sh`](tests/test_notify_guard.sh) has 10 cases: 8 outputs that must be caught and 2 real reports that must pass.

## Delivered, not just attempted

A polling job can look healthy at every step and still lose a message. In the private deployment, a mail checker marked a message seen before it fetched the body; the fetch timed out, the run logged that nothing was new and refreshed its success marker, and the message was never forwarded. [`delivery.py`](delivery.py) reorders those steps and adds a check that does not trust them: `DeliveryLedger.audit` lists the inbox again and compares it with the delivery ledger. A missing message is re-queued and removed from the seen set, so the next poll fetches it even when it is older than the poll's window. If it is still undelivered a grace period later, it is returned once in `escalate`. Escalation is a decision for the caller, meant to go through the [alert gate](reliability.py); the module sends nothing.

[`listener.py`](listener.py) covers the same gap for inbound messages. A process check cannot tell a quiet chat from a listener that stopped polling while its process stayed up. `check` uses two other signals: updates waiting unconsumed at the provider, and whether the update ids the listener recorded are contiguous.

## Scope and limits

- This is a portable extraction and redesign of patterns from my personal system. It is not the complete machine-specific deployment or a production notification service.
- Delivery acknowledgements are caller-reported. The example has no authenticated transport, worker leases, retry scheduler, or provider-side idempotency. A process can die after sending and before acknowledging; a real adapter must handle that uncertainty.
- A `page` decision means eligible to attempt delivery. Replaying that decision is not permission to send twice; consult the outbox. Pending attempts remain pending until explicitly resolved, and recovery cancels them.
- SQLite covers local transaction ordering, not a distributed queue. Inputs and the database are trusted. Clock anomalies reject observations or produce `unknown-clock`; there is no automatic clock repair.
- A fresh success receipt can still describe the wrong task. `job_health` checks freshness, not output correctness, and the notify guard catches runner error text only. A well-formed report about the wrong task passes both; that belongs in a separate task-specific evaluator.
- The notify guard anchors only two patterns, `Error:` and `Execution error`. The other patterns, such as "rate limit exceeded", also hold back a real report that mentions them in its first three lines; the guard errs toward not sending. Whitespace-only output is not treated as empty, and error text below the third line is not checked.
- `schedule.py` works on naive local times and does not model daylight-saving transitions. It does not read plists or logs, and it starts nothing.
- `delivery.py` is at-least-once. The fetch and the send happen outside the database transaction, so a crash after a confirmed send and before the commit sends that message again on the next poll; a real transport needs a dedupe key on the receiving side. It assumes one poller at a time: there is no lease, so two concurrent polls can both send a message. The audit trusts the inbox listing it is given and the ledger. Escalated messages stay queued and keep being retried, and the queue has no retention limit. Message bodies are not stored. A list call that keeps failing, including one that returns a single malformed row, makes every poll and audit return `list-failed` and escalates nothing; the stale `marker()` is the only signal, and checking its age, for example with `job_health(now, ledger.marker(), period)`, is left to the caller.
- `listener.py` takes process state, the waiting count and update ids as caller-reported facts, and assumes the provider numbers a bot's updates one by one. Renumbering is recognized only as an id that goes backwards or jumps more than `max_jump`. Any tick that is not deaf, including `unknown`, resets the deaf count, so a provider that is unreachable every other tick delays a restart indefinitely. A restart is a returned decision; a real watchdog also needs a cooldown and a path for a restart that does not help.
- `state_guard.guard_write` protects only writers that call it, and it treats any loaded `unittest` or `pytest` module as a test run. The runner's before-and-after comparison covers the one state directory it names, by size and modification time; a write elsewhere in the real home, or a change that restores both, is not detected. It uses `pwd`, so it runs on macOS and Linux, not Windows.
- `acceptance.py` takes the receipt as caller-reported facts and does not check who made an observation. Pages are returned for the alert gate, which handles their confirmation and dedupe; the reminder and green notices are returned once and are the caller's to deliver.
- `stores.py` knows only the store paths it is given and a synthetic `jobs` table. A store it was not told about is invisible, which reads as stale rather than healthy. It compares run times across stores and assumes their clocks agree.
- `budget.py` recognizes spend, usage and credit limit messages in English. A rate limit, a timeout or a refusal worded differently is a failure and is charged. A refusal is retried on every tick with no backoff; how often to tick is left to the scheduler.
- Suppression does not erase the observation. Decisions remain in the local event table for inspection. This demo does not dispatch suppressed events to a maintenance system.

The tests are deterministic failure-path checks, not an uptime claim or a load benchmark. No private logs, credentials, contacts, or host-control scripts are needed.

## Related work

[Agent OS](https://github.com/dyjhhh/agent-os) · [Evaluation gates](https://github.com/dyjhhh/agent-eval-gates) · [Security hooks](https://github.com/dyjhhh/agent-security-hooks) · [Feedback loops](https://github.com/dyjhhh/self-improving-loops) · [Award alerts](https://github.com/dyjhhh/award-alert-bridge) · [Careers watch](https://github.com/dyjhhh/careers-watch)

Personal project by Yujia Dong · AI-assisted development. Design choices and review are mine; Claude Code and Codex assisted with implementation, including the public extraction and tests.

MIT license. See [LICENSE](LICENSE).
