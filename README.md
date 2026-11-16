# workmux

A workspace sidebar and sidebar-aware pane layouts for tmux. Start tmux with your
normal configuration, then explicitly enable workmux in the current session.
Workspaces are enabled sessions, tabs are tmux windows, and each window gets a
left sidebar. The remaining area contains ordinary terminal panes.

Workmux reads the **running configuration**, not your config files, and does not
install tmux startup hooks. The sidebar shows tmux alerts; optional Pi, OpenCode,
Codex, and Claude Code integrations add agent-state indicators and desktop
notifications when explicitly installed.

## Requirements

- Python 3.10 or newer, with `curses`.
- tmux 3.3 or newer.
- The installed `workmux` command must be on tmux's `PATH`.

The Python package installs its Tree-sitter dependencies automatically. Agent
harnesses are needed only for their optional integrations; desktop notifications
additionally require a compatible terminal.

## Install

Choose either pipx or Nix; you do not need both.

### pipx

Install from a local clone with [pipx](https://pipx.pypa.io/). From the repository
root, run:

```sh
pipx install .
# To reinstall after updating the source:
pipx install --force .
```

This project is not published to PyPI; use the local repository path. pipx installs
the Python dependencies, but tmux must already be installed separately. Ensure
pipx's application bin directory is on your `PATH`.

### Nix

With Nix's `nix-command` and `flakes` features enabled, run from the repository root:

```sh
nix profile add .
# To update the profile entry after updating the source:
nix profile upgrade workmux
```

On older Nix versions, use `nix profile install .` instead of `nix profile add .`.
The flake supplies Python, its dependencies, and tmux, pinned by `flake.lock`.
Ensure your Nix profile's `bin` directory is on tmux's `PATH`. Nix outputs are
provided for `x86_64-linux` and `aarch64-linux`. Private package dependencies do
not add directories to content shells' `PATH`. Shells or tmux sessions created by
older versions may retain pinned store paths that shadow profile updates; those
existing environments are not automatically repaired.

To build without changing your profile, use `nix build`; the command is then
available at `./result/bin/workmux`. Nix remains optional.

## Enable in your existing tmux session

Start tmux normally. Inside a terminal pane, run:

```sh
workmux enable
```

Workmux infers the socket from `TMUX` and the source pane from `TMUX_PANE`.
It enables **only that session**:

- Adds one sidebar to each existing window, without restarting content processes.
- Preserves the current window, active panes, and zoom state.
- Preserves your prefix, status styling/position, mouse settings, and unrelated bindings.
- Registers a guarded runtime sidebar-focus hook, preserving existing hooks and
  leaving `focus-events` unchanged. No tmux configuration files are edited.
- Adapts supported window-creation, split, and layout bindings while preserving
  their keys, key tables, repeat flags, and notes.
- Leaves existing session names unchanged; the sidebar orders enabled workspaces
  by name and displays their 1-based position as `<index>:<session-name>`, for
  example `1:workmux`. Positions change when workspaces are added, removed, or
  renamed; they are not tmux session IDs.

Bindings are server-wide in tmux. Adapted bindings use a session-local guard:
enabled sessions invoke workmux, while other sessions execute the original tmux
command. The sidebar lists enabled sessions only. Enable other sessions using the
same command if you want them listed as workspaces.

### Activation keybinding

Optionally put this in your own tmux config:

```tmux
bind-key W run-shell -b 'workmux --socket "#{socket_path}" enable --pane "#{pane_id}" --client "#{client_name}"'
```

Press **your prefix**, then **Shift+w**. The explicit socket/pane/client identify
the invoking context. `--client` also displays a concise status message instead
of opening a command-output view for warnings. Run `workmux enable` in a shell
for detailed warnings. Choose another activation key if W is already in use.

This binding is not installed automatically, and no attachment hook is added.

### Optional automatic activation

To enable workmux whenever tmux creates a session, add this to your own tmux config:

```tmux
set-hook -g after-new-session[90] 'run-shell -b "workmux --socket \"#{socket_path}\" enable --pane \"#{pane_id}\""'
```

This covers the first session when the server starts and later new sessions.
The indexed hook preserves other entries; choose another index if `90` is in use.
Reload your config with `tmux source-file ~/.tmux.conf`, then run `workmux enable`
for the current session. Loading the hook does not activate existing sessions.
Workmux must be on the server's PATH. If plugins replace bindings asynchronously,
re-enable after they finish. The hook is opt-in and is never installed by workmux.

### After reloading your tmux config

Run **the same command or keybinding again**:

```sh
workmux enable
```

It reads the newly evaluated bindings, updates supported adaptations, and adds
any missing sidebars. Existing sidebars are not duplicated and wrappers are not
wrapped again. Updated native bindings become the new fallback/original commands.
Unsupported replacements remain untouched, rather than being overwritten with a
stale snapshot. If you unbind a key, re-enabling does not recreate it.

Workmux does not generate or reload tmux configuration; reload your own config
normally, then re-enable. Owned sidebar-focus hooks are not duplicated on
re-enable, and edited or removed owned entries are left alone. If a plugin adds
new window- or pane-local focus hooks after activation, re-enable to extend those
arrays without replacing the plugin's entries.

### Which bindings are adapted?

Workmux supports **single, direct command bodies**:

| Native command | Supported arguments |
| --- | --- |
| `new-window` / `neww` | `-d`, `-n`, `-c`, optional shell command |
| `split-window` / `splitw` | `-b`, `-d`, `-h`, `-v`, `-c`, `-l`, `-p`, optional shell command |
| `select-layout` / `selectl` | Named standard layout, no argument, `-E`, `-n`, or `-p` |
| `next-layout` / `nextl` | No arguments |
| `previous-layout` / `prevl` | No arguments |

Directory and name formats use the invoking content pane. Detached creation
flags, pane sizing, and shell commands retain their native behavior.

Explicit targets (`-t`), full-window splits (`-f`), zoom flags (`-Z`), custom layout
strings, abbreviated layout names, `select-layout -o`, and compound or quoted
command bodies are not adapted. Relevant unsupported direct bindings produce
warnings and remain unchanged. Unparseable live output is also skipped; tmux 3.6a,
for example, does not quote whitespace-containing table names in `list-keys` output.

Menus, shell scripts, conditionals, and command templates are out of scope: their
nested commands are not rewritten, and their presence does not produce warnings
about direct workspace actions. Stock context menus keep their native behavior.
Stock tmux bindings enable without adaptation warnings.

Plugins or includes that already created direct bindings are covered because we
inspect live state. If a plugin later replaces them asynchronously, run enable
again. Plugins and hooks that invoke tmux commands internally are not intercepted
and may move the sidebar; use a workmux layout to repair it afterward.

Sessions sharing windows with other sessions are rejected before activation;
sidebar panes cannot be scoped independently when the underlying window is shared.
Disabling/restoring an enabled session is not implemented in this version.

## Navigation and layouts

Your existing keys remain in effect. With stock tmux bindings, prefix+c creates
a window with a sidebar, prefix+% and prefix+" split content, prefix+Alt+1 through
Alt+7 retile content, prefix+Space cycles layouts, and prefix+E balances adjacent
content panes after enabling.

In the sidebar, click a workspace if tmux mouse support is enabled. The `›`
marker identifies the current workspace; other workspaces and their child rows
use dimmed text and status colors. Workspace selection restores the destination
session's selected tab.

With tmux's `focus-events` option on, workmux's runtime `pane-focus-in` hook returns
focus to the previously active content pane whenever the sidebar gains focus.
Clicks and scrolling still work, including clicking blank space without leaving
the sidebar focused. This is immediate focus restoration, not a non-focusable
pane; keyboard navigation inside the sidebar is unavailable in this mode.
Workmux does not turn `focus-events` on or off. tmux may require detaching and
reattaching clients after you change that option.

With `focus-events` off (tmux's default), focus the sidebar using ordinary pane
navigation and use Up/Down or j/k followed by Enter. Escape/q returns to the
remembered content pane.

When the tree is taller than the sidebar, keyboard navigation scrolls to keep
its selection visible. With tmux's `mouse` option enabled, the mouse wheel over
the sidebar scrolls three rows at a time without changing focus or activating a
workspace or tab. Click a visible row to select it; keyboard navigation resumes
following the highlighted row. Wheel scrolling requires terminal/tmux mouse
support and a curses build that exposes the corresponding wheel buttons.

### Checkouts and worktrees

A workspace may be a repository or a plain container directory holding job
documents and several independent repositories/worktrees. Discovered checkouts
appear as `⎇` rows with paths relative to the workspace's saved directory:

```text
› 1:job
  ├─ ⎇ worktrees/team/backend ● ↑
⣷ │  └─ ✦ 2.0:implement
  ├─ ⎇ worktrees/team/frontend ✓
⚠ │  └─ ✦ 3.0:review
⣷ └─ ✦ 1.0:coordinate
```

Click a checkout row, or select it with Up/Down or j/k and Enter, to open a tab
in that directory **within the parent workspace**. Later selections focus that
associated tab, even if it has been renamed or its shell has changed directory.
Closing the tab allows the next selection to open a fresh one. Discovery itself
creates no tabs, sessions, repositories, branches, or worktrees.

Discovery runs in the background, normally every five seconds. It recognizes
`.git` directories and linked-worktree `.git` files up to six directory levels
below the saved directory, including inside existing checkouts. No particular
directory or branch naming convention is required. Git's registered worktrees
are also checked, subject to the same containment and depth limits. Worktrees
outside the workspace are not listed. Discovery walks directory names only:
file contents and `.git` contents are never scanned. Discovered directory
symlinks are not followed, and missing directories are skipped. Each scan is
limited to 2,000 directories, 16,000 directory entries, 128 Git queries, 64 checkouts,
and a four-second time budget; large trees may therefore yield only a partial
list. Paths containing tabs or
newlines are unsupported, as with workspace directories.

Set the maximum depth globally or override it for one workspace session:

```sh
tmux set-option -g @workmux-checkout-max-depth 6
tmux set-option -t my-workspace @workmux-checkout-max-depth 3
```

Changes trigger a new background scan without restarting sidebars. Depth counts
directory levels below the saved workspace directory: `worktrees/adzerk/bifrost`
is depth 3. A value of `0` disables discovery below the workspace root. Unset,
negative, or invalid values use the default of 6. Higher depths remain subject
to the other scan budgets.

Every checkout has its own Git indicators. The workspace's indicators still
refer only to its saved directory, not an aggregate of nested repositories.
Removing a checkout removes its discovered row, **not its live tabs or content**.
Any agent-state tab it contained remains visible directly under the workspace.
Pushes, PRs, cleanup, and archiving remain outside workmux.

### Git status

Colored symbols follow workspace and checkout names, for example `1:workmux ● ↑`.
They describe the repository containing the workspace's saved directory (the
content pane directory when first enabled), not whichever directory a shell
later changes into.

| Indicator | Color | Git state |
| --- | --- | --- |
| `✓` | Green | Clean; in sync if an upstream is configured |
| `●` | Yellow | Non-conflicting local changes, including staged or untracked files |
| `↑` | Cyan | Ahead of upstream |
| `↓` | Blue | Behind upstream |
| `↕` | Magenta | Ahead and behind upstream (diverged) |
| `!` | Red | Merge conflicts |

All applicable indicators are shown: conflicts first, then non-conflicting local
changes, then upstream status. For example, `! ● ↕` means merge conflicts, other
local changes, and a diverged branch. Divergence uses `↕` instead of separate
`↑ ↓` symbols. `✓` appears only when there are no changes, conflicts, or upstream
differences. Without an upstream, only clean/local changes/conflicts are shown.
Git must be available on the sidebar process's `PATH`; it is optional. Non-Git
folders and unavailable or failed checks show no symbol. Colors fall back to
plain symbols on terminals without color support.

Checks run in the background, normally every two seconds, without fetching or
refreshing Git's index on disk. Ahead/behind compares against the locally known
upstream, so it may be stale until you fetch. Slow checks time out and do not
block sidebar navigation.

### Activity tree

Agent panes and tabs with native tmux alerts appear beneath their workspace.
An agent pane appears beneath a discovered checkout when all root sessions
reported by that pane have explicit roots inside that checkout. Other panes in
the same window do not affect its placement:

```text
› project-a
⣷ ├─ ✦ 1.0:build
⚠ └─ ✦ 2.0:review
  project-b
✓ ├─ ✦ 1.0:changes
  └─ ● 2:logs
```

| Indicator | Color | State |
| --- | --- | --- |
| Filled Braille wheel | Blue | Agent working |
| `⚠` | Red | Agent needs attention: a blocking input dialog, error, or interruption |
| `✓` | Green | Agent finished its run |
| `●` | Yellow | Other tmux alert: bell, monitored activity, or silence |

Agent status symbols appear in the left gutter; the monochrome spark `✦`
identifies agent rows in the tree and dims with inactive workspace labels. The `›` caret marks only the active workspace. Gutter indicators retain
full color even when another workspace's labels are dimmed. Working agents use
an animated filled Braille wheel; the example above shows one frame.
Click a child row to focus that tab, including tabs in other workspaces. Keyboard
navigation with Up/Down or j/k and Enter works for workspace, checkout, and tab rows.
Each active agent pane gets its own row, even when several share a tab. Selecting
an agent row focuses that specific pane. Agent labels use `window.pane:title`
(for example, `2.1:π - workmux`), with tmux's current window and pane indexes.
Rows display the agent pane's terminal title regardless of which pane is focused,
falling back to the tab name. Pane indexes can change as panes are rearranged;
navigation tracks stable pane IDs instead. Workspace names and native-alert tab names are unchanged.
Tabs are listed in window-index order within each group; renaming or reordering
them does not break their click targets. Idle tabs without alerts are hidden,
while discovered checkout rows remain visible.

With several agents in one tab, pending attention is listed before working,
then pending finished. The tab's tmux status indicator uses the same priority. Attention and finished indicators classify native tmux
alerts: clearing an alert removes passive agent rows. Working agent rows remain
visible even without an alert. Native alerts
appear when there is no applicable agent state to show, and all alert indicators
follow tmux's normal clearing behavior.

Agent states come from explicit reports, not from guessing which shell commands
are agents. Dead agent processes are ignored when the integration supplies their
process ID. Checkout grouping uses reported harness roots, never shell cwd,
commands, or tab associations. Agent panes with unknown roots, roots spanning
multiple checkouts, or workspace-level roots stay directly under the workspace,
as do native-alert tabs without an applicable agent report. Bundled integrations report roots automatically; update installed
integrations to get grouping. Custom reporters can pass an absolute `--root`
to `workmux agent state` (repeat `--root` when one reporter aggregates multiple
root sessions).

Install an [agent integration](#optional-agent-integrations) for automatic reports.
Native tmux alerts need no agent integration; their monitoring settings remain
your responsibility.

### Window status indicators

Running sidebars also publish each managed window's activity symbol in the
window-local `@workmux-agent-indicator` option. It uses the same states, priority,
and native alert-clearing rules as the sidebar: an animated Braille wheel for
working, `⚠` for attention, `✓` for finished, and `●` for other native alerts.
Idle windows have an empty indicator. The value is plain text, without colors
or Git status suffixes; window names are unchanged.

To opt in, add the indicator before the index and name in both window formats:

```tmux
set -g window-status-format ' #{?#{&&:#{==:#{@workmux},1},#{@workmux-agent-indicator}},#{@workmux-agent-indicator} ,}#I:#W#F '
set -g window-status-current-format ' #{?#{&&:#{==:#{@workmux},1},#{@workmux-agent-indicator}},#{@workmux-agent-indicator} ,}#I:#W#F '
```

The guard leaves non-workmux sessions with the usual index, name, and native
flags. Adapt the surrounding format to your existing styling; `enable` never
installs or changes these formats for you. Animation uses the sidebar's eight
frames per second without changing `status-interval`, including when it is zero.
One live sidebar publishes batched updates for the server; static indicators
write only when they change. Another sidebar takes over if the publisher exits.
State changes follow the sidebar's normal snapshot refresh.

After updating workmux, run `workmux sidebar reload` to restart existing sidebars
with the new installation. Indicators require at least one running sidebar on
the server.

### Closing tabs and workspaces

When the last content pane in a tab closes, workmux removes that tab's sidebar
and closes the tab. Other tabs in the workspace stay open. Closing the final tab
ends the workspace; there is no empty placeholder and no shell is respawned.

This also applies when the last shell exits naturally or is closed with tmux's
pane-kill command. Cleanup happens on the sidebar's next refresh, normally within
400 ms. Your tmux settings still apply: `remain-on-exit` can retain a dead content
pane, and `detach-on-destroy` controls whether a client detaches or switches to
another session when the workspace ends.

### Optional navigation bindings

You can add explicit navigation bindings yourself, for example:

```tmux
bind-key Up switch-client -p
bind-key Down switch-client -n
bind-key Left previous-window
bind-key Right next-window
bind-key s run-shell -b 'workmux --socket "#{socket_path}" sidebar toggle-focus --pane "#{pane_id}"'
```

Native previous/next workspace navigation includes all sessions in that server,
including sessions not enabled in workmux. No custom workspace reordering UI is
implemented.

Retiling affects only content. The sidebar stays full-height at the left edge.
Retiling preserves pane processes and focus, exits zoom, and can repair a sidebar
misplaced by a raw native layout preset. Mirrored layouts retain the same main
content pane. `main-pane-width` and `main-pane-height` are honored; percentages
refer to the content area. Other native layout tuning options are not implemented.

Balancing (`select-layout -E` or `workmux layout balance`) uses native tmux sibling
balancing inside the content area. It retains the content split structure and
focus rather than switching to a preset, and does not change the remembered
preset used by layout cycling. Already-balanced groups and a single content pane
do not cause tmux to spread the sidebar and content. Pane border status is honored;
balancing with enabled pane scrollbars is currently rejected without changing
the layout. Use a named preset in that case.

## CLI helpers

```sh
workmux workspace list
workmux workspace list --json
workmux workspace new project-b --dir /path/to/project-b
workmux workspace select project-b
workmux workspace select --index 3
workmux tab new editor
workmux split right
workmux split down
workmux layout tiled
workmux layout next
workmux layout balance
workmux sidebar toggle-focus
workmux sidebar reload       # Restart all owned sidebars on the selected server
```

Inside tmux, commands default to its server. Outside tmux, the default server is
named `workmux`. Explicit `--server NAME` or `--socket PATH` overrides inference
and must appear before the subcommand. Pane-oriented commands accept `--pane ID`.
`workspace select` requires an attached client; the sidebar supports one attached
client per session. Use `workspace select --index N` to select by the 1-based
workspace prefix in the sidebar (not a tmux session ID or a checkout/tab row).
Indexes follow the sidebar's lexical session-name order and can change when
workspaces are added, removed, or renamed; prefer names or IDs in scripts.
A positional numeric target still selects a workspace with that name.
Run `workmux --help` or a subcommand's help for argument syntax.
`dispatch` is an internal command for adapted keybindings, not a user workflow.

### Optional isolated-server workflow

You can still create workspaces on a separate server, from outside tmux:

```sh
workmux --server workmux-demo workspace new project-a --dir /path/to/project-a --attach
```

This workflow skips the user's startup config and uses internal workmux defaults:
top tabs, mouse support, prefix+Up/Down for workspaces, Left/Right for tabs, c for a
new tab, s for sidebar focus, %/" for splits, and Alt+1–7/Space for layouts.
New sessions use the requested workspace name without a numeric prefix.
These defaults are not applied by `enable` to an existing server.

After detaching, remove only the demonstration server with:

```sh
tmux -L workmux-demo kill-server
```

## Optional agent integrations

Install an integration on the machine where its harness runs:

```sh
workmux install       # Read-only listing of supported harnesses and disk status
workmux install pi
workmux install opencode
workmux install codex
workmux install claude
# Remove an integration with the corresponding command, for example:
workmux uninstall codex
```

Only install the integrations you use. The listing reports conflicts, incomplete
installations, and available updates; it does not detect installed harness
executables or confirm that an integration is loaded in a running session.
`workmux` must be on the harness's `PATH`. Installation does not change tmux
preferences, harness settings, hook trust decisions, or project configuration.

| Harness | Managed files or entries under its personal configuration directory | Directory override |
| --- | --- | --- |
| Pi | `extensions/workmux-notify.ts` under `~/.pi/agent` | `PI_CODING_AGENT_DIR` |
| OpenCode | `plugins/workmux-notify.js` under `~/.config/opencode` | `OPENCODE_CONFIG_DIR`; otherwise `XDG_CONFIG_HOME/opencode` |
| Codex | Workmux hook groups in `hooks.json` under `~/.codex` | `CODEX_HOME` |
| Claude Code | `skills/workmux/.claude-plugin/plugin.json` and `skills/workmux/hooks/hooks.json` under `~/.claude` | `CLAUDE_CONFIG_DIR` |

Use `--agent-dir PATH` to override the selected harness's personal directory.
With no harness specified, the same override is used for each status entry.

### Pi integration

Run `/reload` in Pi after installing or uninstalling. Test with
`/workmux-notify-test`, then switch away from your terminal within three seconds.
Some terminals suppress desktop notifications while focused.

The extension reports working state at the start of a run, attention while a
blocking agent question/confirmation/input dialog is open, and finished after all
retries and queued continuations. Errors and interruptions report attention.
Closing the dialog returns the agent to working state. Exiting or reloading Pi
clears an idle agent's state; reloading during a run restores working state.

Desktop notifications are also sent for blocking dialogs and completed runs.
A question in the final text reply is still shown as "Finished — ready for input";
the extension does not guess whether prose is a question. Finished means the
agent stopped, not necessarily that the requested task succeeded. State reports
and notifications are disabled in non-interactive/RPC runs.

Sidebar state reporting does not require terminal notification support or tmux
passthrough. Pi calls `workmux` on its PATH to report state in its own tmux pane;
a missing command or reporting failure does not stop the agent.

### OpenCode integration

Restart the local OpenCode TUI after installation/removal. Its global plugin
loader discovers the bundled JavaScript file automatically; no npm dependencies
or config entries are added. Busy/retry events report working, permission and
question dialogs report attention, and idle after a run reports finished. Errors
report attention, and answering/dismissing dialogs restores the underlying state.
Subagent sessions are ignored; multiple root sessions prefer attention over
working over finished. Startup idle events do not send completion notifications.

The integration is for the local, terminal-attached TUI. Headless `run`, `serve`,
`web`, `acp`, and debug invocations are ignored; attaching to a remote server is
not supported. Reports are serialized and best-effort.

### Codex and Claude Code integrations

Codex discovers the personal `hooks.json` automatically. Restart Codex and approve
the workmux hooks in `/hooks`; hooks must be enabled. Workmux never grants trust
or changes `config.toml`. Installation merges workmux's hook groups into an existing
`hooks.json`, preserving unrelated hooks, matchers, and top-level settings. Each
workmux group carries its own checksum; unrelated edits and JSON reformatting do
not prevent updates. Uninstall removes only verified workmux groups, leaving shared
configuration in place (or deleting the file if only workmux's hooks remained).
Invalid/ambiguous JSON and edited workmux groups are conflicts and are left untouched.
Manually added, unmarked commands are not adopted; remove duplicate workmux commands
yourself before installing. Intact older whole-file workmux installations migrate
automatically; edited legacy installations still require manual reconciliation.

Claude Code auto-loads `skills/workmux` as the `workmux@skills-dir` plugin. This
installation mode requires Claude Code 2.1.223 or newer. Run `/reload-plugins` or
restart Claude Code after installation/removal. No marketplace registration or
`settings.json` changes are needed. Do not also load this plugin with `--plugin-dir`,
as that can duplicate its hooks.

Both integrations report working on user prompts/tool hooks, attention on
permission requests and blocking question tools, finished on `Stop`, and idle
on session start/end. Claude also handles permission/elicitation notifications.
After a permission prompt, working resumes at the next tool lifecycle hook, not
necessarily at the instant approval is granted. Child-agent hooks are ignored.
`Stop` means the harness stopped, not that the task succeeded; another stop hook
can still cause it to continue. Errors and interruptions without a corresponding
hook event are not classified separately. Closed agent processes are ignored
through their recorded harness PID, not the short-lived hook process's PID.

Command hooks receive piped stdout, so verifying a real interactive terminal
currently requires **Linux procfs**. The handler verifies the harness's controlling
terminal and foreground process group, then writes through its terminal stdout
descriptor; it does not require the hook process itself to have a controlling
terminal (Claude launches detached hook sessions). Reporting and notifications are skipped if
the harness is headless, remote, backgrounded, has redirected stdout, or its
foreground terminal cannot be verified. Hook stdout stays empty: these commands
never approve permissions, block completion, or emit hook-protocol decisions.
Reporting has a two-second timeout; missing workmux or a closed pane is best-effort.

Discovery/schema checks were validated with OpenCode 1.18.34, Codex 0.146.0, and
Claude Code 2.1.223 on Linux. Tests also cover event payloads and private tmux
state reporting; model-backed end-to-end sessions have not been validated.

### Other agent integrations

Other harnesses can report state explicitly from their tmux content pane:

```sh
workmux agent state working
workmux agent state attention
workmux agent state finished
workmux agent state idle       # Clear the report
```

Use `--pane PANE_ID` for an explicit target and `--pid PID` to tie the report to
the agent process's lifetime. Reports can be made before enabling workmux, but
only enabled workspaces display them. Working reports show immediately; attention
and finished reports classify pending tmux alerts, so also emit a bell or another
monitored alert when reporting those states. The bundled integrations send a bell
automatically. There is one report per pane; agents in separate panes are
aggregated into their tab's indicator.

### Terminal setup

Desktop notifications require a terminal that supports **OSC 9 notifications**.
Enable notifications in that terminal's settings and allow them in your desktop's
notification settings. The integrations also send a separate bell for tmux's
status-bar alert, which requires `monitor-bell on`.

Inside tmux, enable notification passthrough yourself:

```tmux
set -g allow-passthrough all
```

`on` allows visible panes only; `all` also allows hidden panes. Passthrough permits
applications to send terminal controls directly, not only notifications, so use it
only with trusted workloads. Workmux does not change these settings for you.

Over SSH, configure passthrough on the machine running tmux and notification
permissions on your local desktop. Notifications travel through the terminal
connection; no desktop forwarding is needed. A terminal must remain attached;
nested tmux is not supported by these integrations.

### Installation safety

The commands manage only the files or Codex hook groups listed above. Installation
is idempotent and updates intact older workmux copies. Symlinked configuration
directories are supported and their links are left unchanged. Both operations
refuse symlinks at managed file paths. Except for Codex's shared `hooks.json`, files
whose workmux checksum is missing or no longer matches are conflicts; reconcile
edited/conflicting integrations manually first. For bundles, all files are checked
before any are changed. Unrelated files, hooks, extensions, and settings are
preserved. Uninstalling a missing integration creates no directories.

If another integration already sends notifications, disable it before enabling
this one to avoid duplicates. Workmux deliberately leaves other integrations alone.

## Runtime and layout limits

- Sidebars are replicated per window and poll their displayed state every 400 ms.
  Sidebar focus restoration uses tmux events, not that polling interval.
- The tab bar is tmux's status line, at the user's configured position, spanning
  the whole terminal width. Workmux does not impose a theme on existing sessions.
- Sidebars have one blank row at the top and no title. They default to 36 columns
  and shrink in narrow windows; use at least 40 columns. Set the preferred width
  globally in tmux configuration with `set-option -g @workmux-sidebar-width 30`,
  or change it at runtime with `tmux set-option -g @workmux-sidebar-width 30`.
  Existing sidebars adopt runtime changes automatically. Dragging a sidebar border
  does not change the preferred width; it snaps back on the next refresh.
  Workmux always reserves at least 20 columns for content and uses
  at least 4 columns for the sidebar.
- Layouts require at least two cells per pane dimension. Impossible layouts fail
  instead of being applied.
- Raw tmux commands bypass binding adaptations. Run enable again to add sidebars
  to manually created windows or replace a closed sidebar.
- Detaching preserves state. Killing the server loses sessions and workmux's
  in-memory metadata; disk persistence/session restoration is not implemented.
- Bound helper commands invoke `workmux` through the server's PATH. Reinstalls do
  not require new binding paths. Already running sidebar processes retain their
  previous code and internally pinned Python environment.

If the server started without the appropriate application bin directory:

```sh
tmux show-environment -g PATH
tmux set-environment -g PATH "$PATH"
```

Do not remove an old Python environment while a sidebar process still uses it.
After upgrading, run the **updated installation** of workmux to reload every owned
sidebar on the current tmux server:

```sh
workmux sidebar reload
# From outside tmux, select your server explicitly if it is not named workmux:
workmux --socket /path/to/tmux.sock sidebar reload
```

Reload works without an attached client or source pane. It restarts only sidebars
in managed workspaces, keeping pane IDs, layout, focus, zoom, and content processes
intact. It uses the invoking installation's Python environment rather than the old
sidebar startup command. Sidebar navigation state and background caches reset.
Missing sidebars are not recreated; use `workmux enable` for that. Reload does not
reload tmux configuration, reconcile bindings, or reload agent integrations.

## References

See the [tmux manual](https://man.openbsd.org/tmux) and
[tmux hooks documentation](https://github.com/tmux/tmux/wiki/Events) for tmux
configuration details. Run `workmux --help` or a subcommand's help for command
syntax.
