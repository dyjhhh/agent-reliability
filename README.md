# Agent Reliability

[![tests](https://github.com/dyjhhh/agent-reliability/actions/workflows/test.yml/badge.svg)](https://github.com/dyjhhh/agent-reliability/actions/workflows/test.yml)

An agent can be running while its work is failing. I built monitoring and recovery tools for a personal agent system after running into missed jobs, noisy alerts, and checks that trusted process status too much.

This public edition isolates two parts of that work: **deciding when to alert** and **checking evidence that a job actually succeeded**. It runs locally with synthetic observations and sends no messages.

## Try it

Python 3.10 or newer, with no third-party dependencies:

```sh
python3 demo.py
python3 -m unittest discover -s tests -v
```

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

Start with [`reliability.py`](reliability.py), then read the [tests](tests/test_reliability.py) and [design notes](docs/design.md).

## Scope and limits

- This is a portable extraction and redesign of patterns from my personal system. It is not the complete machine-specific deployment or a production notification service.
- Delivery acknowledgements are caller-reported. The example has no authenticated transport, worker leases, retry scheduler, or provider-side idempotency. A process can die after sending and before acknowledging; a real adapter must handle that uncertainty.
- A `page` decision means eligible to attempt delivery. Replaying that decision is not permission to send twice; consult the outbox. Pending attempts remain pending until explicitly resolved, and recovery cancels them.
- SQLite covers local transaction ordering, not a distributed queue. Inputs and the database are trusted. Clock anomalies reject observations or produce `unknown-clock`; there is no automatic clock repair.
- A fresh success receipt can still describe the wrong task. `job_health` checks freshness, not output correctness. That belongs in a separate task-specific evaluator.
- Suppression does not erase the observation. Decisions remain in the local event table for inspection. This demo does not dispatch suppressed events to a maintenance system.

The tests are deterministic failure-path checks, not an uptime claim or a load benchmark. No private logs, credentials, contacts, or host-control scripts are needed.

## Related work

[Agent OS](https://github.com/dyjhhh/agent-os) · [Evaluation gates](https://github.com/dyjhhh/agent-eval-gates) · [Security hooks](https://github.com/dyjhhh/agent-security-hooks) · [Feedback loops](https://github.com/dyjhhh/self-improving-loops)

Built by Yujia Dong with Claude Code and Codex. The public extraction and tests were developed with Codex; design choices and review remain my responsibility.

MIT license. See [LICENSE](LICENSE).
