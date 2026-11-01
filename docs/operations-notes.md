# Operations notes

Notes from the private deployment; the scripts are not published.

The always-on agent runs on a Mac mini that nobody logs into. Each section below exists because something on that machine failed silently at least once. Where this repository has a portable, tested version of an idea, the section links to it.

## One gate for alerts

Ten alerting scripts used to message me directly. Most now pass their message through one gate script; a few older watchdogs still message me directly. A white signal never pages. Nothing pages in the first twenty minutes after boot. Nothing pages while the kernel TIME_WAIT table holds more than twelve thousand entries (see the last section), because every probe then fails for a reason that is not the thing being probed. A red or yellow alert needs confirming runs, two by default, at least three minutes apart within six hours. The same class is not repeated for six hours unless it escalates from yellow to red. Every suppressed alert is appended to a handoff file so an agent can fix the cause, and a green run clears the class.

Gate state is guarded by atomic `mkdir` locks instead of `flock`, because macOS does not ship the `flock` command.

[`reliability.py`](../reliability.py) reimplements this policy with transactional state and synthetic tests. It is not the deployed script.

## A runner's error is not a report

A scheduled job that runs an agent and forwards its output will forward the runner's own error text when the runner fails. One morning the mail summary arrived as the single line `Error: Reached max turns (5)`. The same failure class had been fixed three weeks earlier in the weekly report pipeline, and that check was never copied to the mail job. A check that lives inside one script protects only that script. [`guards/notify-guard.sh`](../guards/notify-guard.sh) is the check as a standalone function with ten regression cases, including a real report that quotes "Error:" and must still pass.

## Marked seen is not delivered

A checker polled an email inbox and forwarded each new message to me through a chat bot. It wrote a message's id to its seen list before fetching the body. On 2026-09-29 one fetch timed out. The run logged that there was nothing new, refreshed its success marker, and the message was never forwarded or retried. Two other paths had the same shape: a failed list call read as an empty inbox and still refreshed the marker, and the send result was never checked, so a failed send was logged as sent. Every health signal measured a step (the list call worked), and none measured whether the message reached me.

A message is now marked seen only after its body is in hand and the send returned ok, a failed list call leaves the marker alone, and each delivery goes into a ledger. A daily audit lists the inbox, compares it with the ledger and re-queues anything missing for the next run. It escalates only when a re-queued message is still undelivered twenty hours later. The same investigation found that the daily watchdog's alerts could never fire: it ran once a day behind the gate's default of two confirmations within six hours. Its sustained alerts, the red no-success alert and the delivery escalation, now need one confirmation. [`delivery.py`](../delivery.py) reimplements the ordering, the ledger and the audit with synthetic tests.

## Checker of checkers

The watchdog that keeps the always-on agent alive found the agent with a process-name pattern. When the agent's command line changed, the pattern stopped matching. The watchdog decided the agent was dead and restarted a healthy agent every fifteen minutes for a day. An hourly self-test now verifies that the watchdog's pattern still matches a live process and scans the scripts for the retired pattern. A checker that nobody checks fails in the direction of confidence.

## `pgrep -f` counts the tmux wrapper

`pgrep -f` matches full command lines, so it also matches the `sh -c` wrapper that tmux starts around the agent. One healthy agent counted as two, a duplicate check killed it, and the session went down. Process counts now match the agent binary only, and the recovery path never kills anything.

## Evidence that a job ran

"Is this job running?" is answered from evidence of work, not from `launchctl list`. For each job the audit reads the log its plist declares, the commits it produces if it is a sync job, whether its schedule is an interval or a calendar, and a list of non-zero exit codes that are designed outcomes. Three rules came from wrong verdicts:

- launchd creates a job's declared stdout log when it loads the job and leaves it empty for a job that prints nothing when healthy. A zero-byte log is the absence of evidence: its modification time is the load time, not a run.
- A job cannot be late for a run it was never scheduled to make. A job installed or changed less than one period ago is not yet due.
- An interval job that writes only when something happens leaves sparse evidence, so it is called stale only after three periods.

Python's XML parser was broken in that machine's Python build, so plists are read through `plutil`. [`schedule.py`](../schedule.py) has the verdict logic as pure functions over synthetic jobs, including the reverse check that a job which genuinely stopped is still reported stale.

## Calendar jobs without a GUI login

Without a GUI login, launchd fires interval jobs but not calendar jobs. A dispatcher that runs every five minutes on an interval trigger reads every plist and replays any calendar slot that has passed, inside a two-hour catch-up window, at most once per slot per day, and yields to launchd for any job loaded in a GUI session. Adding or changing a scheduled job means editing one plist. The first version kept a hand-written table of seven jobs; a later audit found 25 calendar jobs, and 18 of them had been silently skipped. The current version has no table. `due_slots` in [`schedule.py`](../schedule.py) is the replay rule without the I/O.

## Reboot without sudo and without auto-login

After a reboot the Mac mini sits at the login window. The user launchd domain does not exist there, so nothing can be bootstrapped, and the agent's tmux session is gone. The recovery chain uses no sudo and no auto-login:

1. A script on the laptop polls the Mac mini every ten minutes. It holds an atomic `mkdir` lock, counts only real agent processes (see the `pgrep -f` section) and never kills anything.
2. Over SSH it creates the user domain and starts the agent's tmux session.
3. It bootstraps every plist into `user/<uid>`. This works only because each plist declares `LimitLoadToSessionType = Background`; without that key the bootstrap fails with an I/O error.
4. An emergency runner covers the gap and steps aside once the jobs are loaded.

## Liveness from the pane, not the process

The liveness guard and the stuck detector read the tmux pane, not the process table. A working session shows a working indicator. A stuck session shows the indicator unchanged for two consecutive checks. An idle session shows an empty prompt. The detector learned to ignore the dim placeholder suggestion that Claude Code prints in an empty prompt, which once read as unsubmitted input and caused a restart loop. The [weekly brief script](https://github.com/dyjhhh/agent-os/blob/main/agents/weekly-brief.sh) in Agent OS waits for the same idle state before injecting its next prompt, instead of sleeping for a fixed time.

## Alive is not receiving

On 2026-09-28 the chat bot's inbound log had been silent for five days, and the watchdog called the listener healthy. It checked only that the listener process existed, and the listener can stop polling after repeated conflicts while its process stays up. Showing that the silence was real took a manual investigation: the connection to the provider was open with steady traffic, nothing was waiting at the provider, and no second consumer was reading the same bot. Nobody had written.

The watchdog now also asks the provider how many updates are waiting. A live poller takes an update within seconds, so updates still waiting on three consecutive ticks, about four to six minutes, mean nobody is consuming them, and the existing silent restart runs. An unreachable provider gives no verdict. A separate log-only check reads the update ids the listener recorded: the provider numbers a bot's updates one by one, so the first id after a long silence shows whether the chat was quiet or updates went somewhere else. Ids can be renumbered after a long idle period, so an id that goes backwards or jumps very far is logged as inconclusive. [`listener.py`](../listener.py) is the classification as a pure function over synthetic ticks.

## Sync bridge

When the Mac mini cannot reach GitHub but the laptop can, a bridge script pulls the Mac mini's commits over Tailscale, pushes them to origin and pushes origin back to the Mac mini. It commits any in-flight edits on the Mac mini first, so a rebase cannot lose them.

## Kernel TIME_WAIT exhaustion

The Mac mini's kernel accumulated 21,267 TIME_WAIT entries that never expired, against 16,384 ephemeral ports. New connections to some hosts failed while others worked, which looked like three unrelated outages: GitHub, cloud storage and a third external service. No process restart clears this; only a reboot does. The count is now a red line in the job audit, a suppression condition in the alert gate and the first field in the weekly health score. In [`reliability.py`](../reliability.py) the same idea is the `host_healthy=False` input: a degraded host suppresses alerts and does not build confirmation history.

## Tests must not see the real home

On 2026-10-26 a test run used the operator's real `HOME`. One fixture recorded a failure and a recovery a millisecond apart, and the code under test wrote both into the production failure history. The health check reads that history, so it sent three "recovered" notices for outages that never happened. The tests passed; the damage was outside the test directory. A fix was added to that one file's writer, which protected that file and nothing else.

Two rules came out of it. Every suite runs through a wrapper that creates a fresh temporary `HOME`, starts the tests with `env -i` so no token or stray variable is inherited, and compares the size and modification time of the real state files before and after the run; a change fails the run. State writers also refuse, while a test framework is loaded, to write anywhere inside the account's real home, which they look up from the password database rather than from `$HOME`. A test that forgot to isolate itself is then refused instead of trusted. Two symptoms, a failure and a recovery a millisecond apart or history timestamps out of order, now point first at a leaking test. [`state_guard.py`](../state_guard.py) and [`tests/run-isolated.sh`](../tests/run-isolated.sh) are the portable version, and `make test` runs every Python suite through the wrapper.

## Expired evidence is not an outage

The alert system keeps a record of end-to-end tests: a person confirms that the phone alarm actually sounded, that the desktop alarm played and that a text message arrived. Each observation is valid for a few days. When one expired, the health check reported it through the same path as a broken channel, and for about 32 hours the operator was paged repeatedly about an outage that did not exist. Every channel was working; only the proof had aged out.

Expired, missing or incomplete evidence now goes to a separate reminder: one yellow "retest due" notice per newly due set of items and one green notice when every announced item passes again. A test recorded as failed is still an incident and still pages, it never turns into a reminder by getting old, and it never produces the green notice. An unreadable record is unknown: it changes nothing rather than reporting every item as untested. [`acceptance.py`](../acceptance.py) is the classification and the reminder state as pure functions.

## The scheduler moved its state

A desktop agent app runs some scheduled jobs on the laptop and records each run in a SQLite file. On 2026-10-30 an automatic update of the app moved that record to a new database file. The old file stopped changing at its last run. The health check still read the old file, so it reported the scheduler as stalled while the jobs kept running.

The check now opens every store it knows about, read-only, and uses the one with the newest recorded run, and it writes down which store the verdict came from. A store only records runs that happened, so taking the newest cannot hide a real stall: a stall that shows in every store still pages, and a pause recorded in the live store is not hidden by an old active row in the frozen one. A store name the check does not know about still reads as stale, which is the loud direction. [`stores.py`](../stores.py) is the selection rule over a synthetic table.

## A refused run is not an attempt

A scheduled job sends a queued batch of items to a hosted model for scoring, with a daily cap on items and two attempts per input window. On 2026-10-28 the provider refused a run with a spend-limit message: exit status 1, nothing scored, no tokens used. The job's ledger recorded it like any other run, so the whole batch was charged against the day's cap and the run counted as an attempt. The rest of that day's work did not run.

A run that scored nothing, exited non-zero and printed a spend, usage or credit limit message is now recorded as a refusal. It is not charged to the daily cap or to the attempt count, and the queue and the watermark stay where they were, so the next tick retries the same window. A run that scored anything is partial and is charged as before. [`budget.py`](../budget.py) is the classification and the accounting as pure functions.
