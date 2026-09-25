# Operations notes

Notes from the private deployment; the scripts are not published.

The always-on agent runs on a Mac mini that nobody logs into. Each section below exists because something on that machine failed silently at least once. Where this repository has a portable, tested version of an idea, the section links to it.

## One gate for alerts

Ten alerting scripts used to message me directly. Most now pass their message through one gate script; a few older watchdogs still message me directly. A white signal never pages. Nothing pages in the first twenty minutes after boot. Nothing pages while the kernel TIME_WAIT table holds more than twelve thousand entries (see the last section), because every probe then fails for a reason that is not the thing being probed. A red or yellow alert needs confirming runs, two by default, at least three minutes apart within six hours. The same class is not repeated for six hours unless it escalates from yellow to red. Every suppressed alert is appended to a handoff file so an agent can fix the cause, and a green run clears the class.

Gate state is guarded by atomic `mkdir` locks instead of `flock`, because macOS does not ship the `flock` command.

[`reliability.py`](../reliability.py) reimplements this policy with transactional state and synthetic tests. It is not the deployed script.

## A runner's error is not a report

A scheduled job that runs an agent and forwards its output will forward the runner's own error text when the runner fails. One morning the mail summary arrived as the single line `Error: Reached max turns (5)`. The same failure class had been fixed three weeks earlier in the weekly report pipeline, and that check was never copied to the mail job. A check that lives inside one script protects only that script. [`guards/notify-guard.sh`](../guards/notify-guard.sh) is the check as a standalone function with ten regression cases, including a real report that quotes "Error:" and must still pass.

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

## Sync bridge

When the Mac mini cannot reach GitHub but the laptop can, a bridge script pulls the Mac mini's commits over Tailscale, pushes them to origin and pushes origin back to the Mac mini. It commits any in-flight edits on the Mac mini first, so a rebase cannot lose them.

## Kernel TIME_WAIT exhaustion

The Mac mini's kernel accumulated 21,267 TIME_WAIT entries that never expired, against 16,384 ephemeral ports. New connections to some hosts failed while others worked, which looked like three unrelated outages: GitHub, cloud storage and a third external service. No process restart clears this; only a reboot does. The count is now a red line in the job audit, a suppression condition in the alert gate and the first field in the weekly health score. In [`reliability.py`](../reliability.py) the same idea is the `host_healthy=False` input: a degraded host suppresses alerts and does not build confirmation history.
