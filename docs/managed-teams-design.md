# Managed team panes: design and feasibility

Status: first implementation complete. Managed mode is opt-in and does not
add a Peloton dependency or a profile configuration file.

See the README for usage. This document records the design, implementation
constraints, and feasibility probes.

## Goal

Keep a master's Pi and two worker panes in the same workmux tab while the
master explicitly replaces the workers between phases. For example, workers
can read the project and write only `spec/` during design, then start fresh,
read the agreed spec, and write only `src/` during implementation. The spec on
disk is the handoff; the old conversations are not. Give validation tools a
writable scratch directory if they would otherwise write outside the phase's
allowed directories. This is a workflow guardrail, not a security boundary.

## Responsibilities

- **Workmux** owns opted-in worker launches and resets, not the design or
  implementation workflow. Keep the sidebar, master process, pane IDs, and
  layout intact. Refuse to overwrite a missing, moved, or repurposed worker
  pane. Use an explicit opt-in at `team new` in the current tab for the first
  version; don't adopt existing teams or require a dedicated new tab.
- **The launcher** is a trusted, user-selected executable, not a language for
  configuring Bubblewrap. Save the initial absolute checkout directory at team
  creation, use it for every launch, and pass the profile as one argument, the
  worker role in `WORKMUX_TEAM_ROLE`, and a unique ID in
  `WORKMUX_TEAM_LAUNCH_ID`. The profile starts a fresh Pi conversation
  and selects the writable paths. It must keep the terminal attached to the
  foreground program; preserve `TMUX`, `TMUX_PANE`, the launch ID, and access
  to the notification extension and tmux socket; and stop its work on teardown
  rather than leave any old jobs running. For this workflow it must keep
  host-visible PIDs and access to the same Peloton socket directory. Sharing
  the network namespace is not required for Peloton's pathname Unix sockets.
  The launcher need not print anything for workmux to consume.
- **`workmux-notify`** reports `launch-id:process.pid` over `TMUX` and
  `TMUX_PANE` on an interactive `session_start` with reason `startup`, and only
  when `WORKMUX_TEAM_LAUNCH_ID` is set. In the same tmux command queue, write
  the pane-scoped registration only if the expected launch ID matches and no
  registration has been accepted yet. Keep it separate from activity (idle
  clears that state), silent, and best-effort. Don't register on `/reload` or
  conversation changes, or clear the record on shutdown: workmux owns
  expiry and invalidation. Registration means **Pi started**, not **Peloton
  reachable**.
- **The master and Peloton** own peer discovery, messages, validation, and
  phase transitions. The master sends kickoff to worker 1's new PID and
  includes worker 2's new PID. The master handles startup timing on the
  messaging side, without blindly resending a message whose delivery status
  is unclear. Workmux does not depend on or query Peloton.

## CLI and launcher contract

```sh
workmux team new --launcher /path/to/launch-worker --profile design
workmux team reset --profile implementation
workmux team status --json
```

- `team new`: `--launcher` opts in and requires an initial `--profile`. Save
  the resolved executable and reuse it with each selected profile as its first
  argument (`launch-worker design`, not a shell-parsed command string). Keep
  the existing foreground-shell requirement. Wait for both workers to register
  before replacing that job with the master Pi. On setup failure keep the
  original shell and roll back only still-owned panes created by that call.
  Plain `team new` starts an unmanaged team.
- `team reset`: the master invokes this through Pi's `bash` tool. Resolve the
  original master pane via `TMUX` and `TMUX_PANE`; don't require the calling
  subprocess to be in the foreground, and don't send terminal input. Reuse
  the saved launcher and directory. Success writes one JSON pair to stdout;
  failure is nonzero with diagnostics on stderr and no partial pair on stdout.
- `team status --json`: a read-only way to get the initial pair once the master
  starts or inspect the current generation after a reset. Both commands use
  `{"slave 1": 172, "slave 2": 173}` for a complete, current pair; status prints
  `null` when no pair is available. Human-readable status remains the default. `team refresh` and
  `team clear` keep their existing meanings; clear must not stop any process.

The script interprets profile names such as `design` and `implementation`.
Workmux validates the saved executable, checkout, and pane ownership before
stopping anything; it doesn't know whether a profile name or its mount rules
are valid inside the script. A launcher that rejects a profile is a startup
failure. Workmux doesn't capture or parse the pane's terminal output.

## Reset protocol

1. Resolve the opted-in team from the master's stable pane ID. Unlike initial
   `team new`, reset must work from a subprocess of the master Pi with stdout
   captured; don't require the invoking job to own the pane's foreground TTY.
   Serialize mutations for this team (server and team incarnation) with a
   crash-released lock, such as a private file lock. Keep enough transition
   state to recognize an interrupted reset. A bare tmux `wait-for -L` lock is
   not released when the acquiring client exits.
2. Preflight the launcher, checkout, and ownership of both workers before
   stopping either.
   Store ownership separately from Pi registration: stable pane and window IDs
   and the tracked pane leader's process incarnation (PID plus start identity),
   not just the pane ID or a persistent tmux option. Stamp the new pane leader
   in the same tmux command queue as respawn. Guard each destructive operation
   against the expected window, pane leader, and launch state. A manual tmux
   change can still invalidate a transition; do not treat the reset as an
   atomic tmux transaction.
3. Invalidate the previous pair and reserve new launch IDs. Stop both old
   workers before starting either new one, without sending terminal input.
   For initial creation, reserve each pane with an explicit workmux-owned
   process rather than an empty command. For a reset, pane-local
   `remain-on-exit` and respawn into a short-lived no-op leave a dead pane in
   place. Use `pane_dead`, not the presence of `pane_pid`, to identify dead
   panes and respawn them without `-k` so a manually started live job is not
   replaced. Confirm that the tracked launcher and Pi processes exited before
   the next round (Linux pidfds work for exact-process waits). This does
   **not** guarantee cleanup of descendants that outlive those processes.
   Supporting such jobs requires additional supervision.
4. Start both profiles. In one final snapshot verify both pane assignments,
   live process identities, and registrations for this reset. Return a
   machine-readable role-to-PID pair on the **reset command's** stdout, not the
   launcher's. Status also exposes a pair only after its generation is
   committed. On timeout or a one-worker failure, report per-worker
   diagnostics, close the pending launch IDs to late registrations, and leave
   owned panes available for inspection and an explicit retry. After an
   interruption, recognize the pending transition; retry only if ownership
   and teardown can still be verified, and otherwise refuse. Don't silently
   restart the old phase, recreate missing panes, or kill a pane whose
   ownership is lost. Returned PIDs describe the pair at the time of the
   snapshot, not a liveness guarantee.

## Implementation and validation

- `workmux/cli.py` owns the command syntax and `workmux/teams.py` owns team
  lifecycle. Keep the window-scoped role assignment and pane border formats
  compatible. Use a private lock per tmux server incarnation and window, and
  store the pending/committed generation in a window option. Worker pane
  options track the launcher incarnation independently of Pi registration.
  The Linux worker entry point uses a one-use spawn token, so a bare tmux
  respawn cannot replay a managed launch. If interruption makes ownership or
  teardown ambiguous, refuse rather than guessing. Clear team metadata without
  killing a process or clobbering user-edited options.
- Raise the workmux-wide minimum to **tmux 3.8**. Keep the runtime diagnostics
  (`workmux/core.py`), `README.md`, and `AGENTS.md` in sync. Target Linux for
  managed teams only. Keep Bubblewrap and Peloton out of workmux's runtime
  dependencies. No sidebar team entry, automatic restart, or phase engine.
- Extend the Node tests for `workmux/resources/pi-notify.ts`: startup-only
  registration, expected ID guard, and unchanged notification/activity/reload
  behavior. Extend private tmux integration and completion tests: unchanged
  unmanaged path; literal launcher arguments and checkout paths; successful
  in-place reset; partial startup and retry; stale registration; concurrent
  reset; interrupted reset with no usable pair; and refusal to overwrite a
  manually repurposed pane. Check pane IDs, layout, focus, zoom, and master PID.
- Use disposable sockets and harness directories, with no model calls or
  installation in a user's real profile. Run `scripts/check.sh`, the isolated
  `scripts/check-install.sh` with pipx, and `git diff --check`. The feasibility
  probes below are evidence, not committed acceptance tests.

## Feasibility results

Probes used temporary directories/private tmux servers on Linux. The real Pi
probe used isolated HOME and XDG directories, no session history, and no model
calls. No user server, profile, or harness configuration was changed.

| Probe | Result |
| --- | --- |
| Bubblewrap 0.13.1, read-only checkout with `spec/` or `src/` overmounted read-write | Writes succeeded only in the selected part of the checkout. An independent mount probe confirmed the host PID and network namespaces are shared with the proposed flags. |
| tmux 3.8, stop into a no-op with pane-local `remain-on-exit`, relaunch two Bubblewrap stand-ins | Preserved layout, pane IDs, focus, and master PID, with the master both zoomed and unzoomed. Checked the old foreground worker processes exited before the new write profile. Empty respawn with default settings instead removed the worker pane. A fast nonzero startup exit with `remain-on-exit` preserved the pane; argv launch supported a literal working path containing spaces and tmux/shell metacharacters. |
| Pi 1.7.0, two interactive starts behind Bubblewrap with a temporary registration extension | Both valid launch IDs registered against the private tmux socket; the wrong-ID and unopted starts did not. Pi's reported PID was the actual foreground Pi child, not tmux's `pane_pid` (Bubblewrap). Don't gate on `pane_current_command=pi` or infer startup args from Pi's rewritten process title. |
| Concurrent registration clients on tmux 3.8 | A write guarded by the expected launch ID and empty record accepted exactly one of eight simultaneous writers across 20 rounds. Old IDs racing a new launch and late matches after closing registration were refused. This validates the registration primitive, not the full reset. |
| Manual tmux respawn between launches | The pane ID and pane-local registration survived even though the pane leader changed. A tmux-side guard checking the old leader refused to overwrite the replacement. |
| Detached worker descendant with shared PID namespace and `bwrap --die-with-parent` | A deliberately detached child survived replacing the pane, even after the foreground worker was gone. A plain respawn isn't a process-tree teardown mechanism. |
| tmux `wait-for -L` | A client exited after taking a lock; another client could not acquire it until explicitly unlocked. |

### Follow-up probes

- **Real-Pi phase swap and retry (Linux, Pi 1.7.0, tmux 3.8, Bubblewrap
  0.13.1).** Three disposable interactive Pi processes, with a temporary
  startup extension in place of production registration. Between phases the
  probe observed both old Pi and Bubblewrap process handles exit, kept the
  master alive and the layout/focus unchanged, and verified that the new
  workers could read the design handoff but write only within `src/` in the
  checkout. A second worker exiting with status 23 left its pane intact, did
  not produce a pair, and was guarded against late registration. An explicit
  retry recovered from one live owned worker and one dead pane and obtained
  both new PIDs.
- **tmux compatibility.** tmux 3.7b removed a pane created with an empty split
  command; 3.8 retained it. An explicit reservation command, same-queue
  `set-option -pF` leader recording, pane-local `remain-on-exit`, and non-forced
  respawn of a dead pane worked on both. When a worker exited, `pane_pid`
  retained its value on 3.7b and was blank on 3.8. The managed protocol should
  rely on `pane_dead`; tmux 3.7b is not a support target.
- **Interrupted reset.** A private file lock excluded a second process and
  was released on `SIGKILL` after just one worker had been parked. The
  private tmux server retained pending intent and the separate pane owners;
  pending intent prevented publishing the two stale replies as a pair. A
  manual respawn then changed the other worker's leader and the stop guard
  refused to touch it. This is a controlled interruption, not a test of every
  possible crash point.
- **Actual Peloton routing (Peloton 0.1.0, `e767486`, same Pi/tmux/Bubblewrap
  versions).** With isolated agent directories and a shared temporary socket
  directory and PID namespace, three real Pi TUIs queued passive `nextTurn`
  messages master → worker 1 → worker 2 → worker 1 → master before
  and after replacing both workers. Sender/recipient identities and received
  envelopes matched, including reply IDs on both return hops. The new Pi
  processes had no old queued messages; sends to the old PIDs failed even
  though abruptly stopped peers left socket files until cleanup. No agent
  turns ran and the test did not exercise wake delivery or model handling.
  Workmux should not manage Peloton socket cleanup.

**Assessment:** The core lifecycle, failure/retry path, and passive messaging
are feasible with an opt-in foreground profile and no workmux–Peloton
coupling. Keep full process-tree teardown out of scope for the simple
shared-PID launcher. These probes predate the implementation and are not
acceptance tests or model-backed workflow tests. Use the tested tmux 3.8 as
the minimum for all workmux workflows.
