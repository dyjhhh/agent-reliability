# Agent Reliability

[![tests](https://github.com/dyjhhh/agent-reliability/actions/workflows/test.yml/badge.svg)](https://github.com/dyjhhh/agent-reliability/actions/workflows/test.yml)

An agent can be running while its work is failing. I built monitoring and recovery tools for a personal agent system after running into missed jobs, noisy alerts, and checks that trusted process status too much.

This public edition isolates four parts of that work: **deciding when to alert**, **checking evidence that a job actually succeeded**, **replaying calendar jobs the scheduler skipped**, and **refusing to send a runner's error text as a report**. It runs locally with synthetic observations and sends no messages.

## Try it

Python 3.10 or newer and bash, with no third-party dependencies:

```sh
python3 demo.py
python3 -m unittest discover -s tests -v   # 28 unittest cases for reliability.py and schedule.py
bash tests/test_notify_guard.sh            # 10 bash cases for guards/notify-guard.sh
```

`make test` runs both, plus 5 unittest cases for the pattern scanner in `tools/`.

The demo walks through boot suppression, a degraded host, spaced confirmation, a failed delivery, a retry, deduplication, escalation, and recovery. Its SQLite database lives in a temporary directory and is removed afterward.

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

Start with [`reliability.py`](reliability.py), then read the [tests](tests/test_reliability.py) and [design notes](docs/design.md). [`schedule.py`](schedule.py) and its [tests](tests/test_schedule.py) cover scheduling. [Operations notes](docs/operations-notes.md) record lessons from the private deployment, whose scripts are not published.

## Where the output guard fits

`job_health` and `audit` ask whether a job left fresh evidence of success. Neither looks at what the job produced. [`guards/notify-guard.sh`](guards/notify-guard.sh) covers one narrow failure in that gap: a runner that hits its turn limit or loses authentication still prints text, and a job that forwards whatever was printed sends the runner's own error message as the report. The guard runs just before sending. It checks the first three lines for runner and API error text, treats empty output as a failure, and lets a real report that quotes "Error:" in its prose through. [`tests/test_notify_guard.sh`](tests/test_notify_guard.sh) has 10 cases: 8 outputs that must be caught and 2 real reports that must pass.

## Scope and limits

- This is a portable extraction and redesign of patterns from my personal system. It is not the complete machine-specific deployment or a production notification service.
- Delivery acknowledgements are caller-reported. The example has no authenticated transport, worker leases, retry scheduler, or provider-side idempotency. A process can die after sending and before acknowledging; a real adapter must handle that uncertainty.
- A `page` decision means eligible to attempt delivery. Replaying that decision is not permission to send twice; consult the outbox. Pending attempts remain pending until explicitly resolved, and recovery cancels them.
- SQLite covers local transaction ordering, not a distributed queue. Inputs and the database are trusted. Clock anomalies reject observations or produce `unknown-clock`; there is no automatic clock repair.
- A fresh success receipt can still describe the wrong task. `job_health` checks freshness, not output correctness, and the notify guard catches runner error text only. A well-formed report about the wrong task passes both; that belongs in a separate task-specific evaluator.
- The notify guard anchors only two patterns, `Error:` and `Execution error`. The other patterns, such as "rate limit exceeded", also hold back a real report that mentions them in its first three lines; the guard errs toward not sending. Whitespace-only output is not treated as empty, and error text below the third line is not checked.
- `schedule.py` works on naive local times and does not model daylight-saving transitions. It does not read plists or logs, and it starts nothing.
- Suppression does not erase the observation. Decisions remain in the local event table for inspection. This demo does not dispatch suppressed events to a maintenance system.

The tests are deterministic failure-path checks, not an uptime claim or a load benchmark. No private logs, credentials, contacts, or host-control scripts are needed.

## Related work

[Agent OS](https://github.com/dyjhhh/agent-os) · [Evaluation gates](https://github.com/dyjhhh/agent-eval-gates) · [Security hooks](https://github.com/dyjhhh/agent-security-hooks) · [Feedback loops](https://github.com/dyjhhh/self-improving-loops)

Personal project by Yujia Dong · AI-assisted development. Design choices and review are mine; Claude Code and Codex assisted with implementation, including the public extraction and tests.

MIT license. See [LICENSE](LICENSE).
