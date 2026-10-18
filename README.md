# workmux

A workspace sidebar and sidebar-aware pane layouts for tmux. Start tmux with your
normal configuration, then explicitly enable workmux in the current session.
Workspaces are enabled sessions, tabs are tmux windows, and each window gets a
left sidebar. The remaining area contains ordinary terminal panes.

Workmux reads the **running configuration**, not your config files. It does not
track agents or install startup hooks. An optional Pi extension can send desktop
notifications; it is installed only when explicitly requested.

## Requirements

- Python 3.10 or newer, with `curses`.
- tmux 3.3 or newer. Tests currently run against tmux 3.6a on Linux;
  macOS/Ghostty validation is still needed.
- The installed `workmux` command must be on tmux's `PATH`.

Python dependencies are Tree-sitter and its tmux grammar. They parse tmux's
normalized `list-keys` output, not the original `tmux.conf`.

## Install

Install from this checkout with [pipx](https://pipx.pypa.io/):

```sh
cd /work/repos/micha/workmux
pipx install .
# After source changes:
pipx install --force /work/repos/micha/workmux
```

This project is not published to PyPI; use the checkout path. pipx installs the
Python dependencies, but tmux must already be installed separately. Ensure pipx's
application bin directory is on your `PATH`.

### Local Nix reinstall

On this workstation, the executable, gitignored `reinstall-local.sh` builds the
application with Python dependencies and tmux on its runtime `PATH`. It replaces
only workmux in the selected Nix profile and rolls back a failed replacement.

```sh
./reinstall-local.sh
# Build without installing, or test a separate profile:
./reinstall-local.sh --build-only
./reinstall-local.sh --profile /tmp/workmux-test-profile
```

The script is machine-local and is not distributed with the repository. Choose
pipx or the Nix profile installation; installing both can cause PATH shadowing.
The grammar's Nix derivation is defined in `nix/python-deps.nix`.

## Optional Pi notifications

Install the bundled personal extension on the machine where Pi runs:

```sh
workmux install       # List supported harnesses and installation status
workmux install pi
# Later, to remove it:
workmux uninstall pi
```

The listing is read-only and reports the managed extension's on-disk status,
including conflicts and available updates. It does not detect whether Pi itself
is installed or whether the extension is enabled in a running session.

Run `/reload` in Pi after either command. Test with `/workmux-notify-test`, then
switch away from Ghostty within three seconds; Ghostty may suppress desktop
notifications while focused.

The extension notifies when Pi opens a blocking question/confirmation/input dialog
and when the agent settles after all retries and queued continuations. It also
reports errors and interruptions. A question in the final text reply triggers the
same "Finished — ready for input" notification. A settled run is not proof that
the requested task succeeded. Non-interactive/RPC runs emit no terminal escapes.

Notifications use OSC 9, with tmux passthrough when running inside tmux, plus a
separate bell for tmux's status-bar alert. For Ghostty over SSH, enable local OS
notification permissions and Ghostty's `desktop-notifications` setting (enabled
by default). In the remote tmux server, enable passthrough yourself:

```tmux
set -g allow-passthrough all
```

`on` allows visible panes only; `all` also allows hidden panes. Passthrough permits
applications to send terminal controls directly, not only notifications, so use it
only with trusted workloads. tmux bell alerts require `monitor-bell on`. Desktop
notifications require an attached terminal; nested tmux is not supported by this
extension. These commands do not change tmux options or configuration.

The commands manage only `extensions/workmux-notify.ts` under `~/.pi/agent`, or
`PI_CODING_AGENT_DIR` when set. Use `--agent-dir PATH` to override that location.
They do not modify Pi settings, project extensions, or other extensions.
Installation is idempotent and updates intact older workmux copies. Both commands
refuse symlinks and files whose workmux checksum is missing or no longer matches;
move an edited/conflicting file aside manually first. Uninstalling a missing
extension is a no-op.

If you already use the earlier standalone `desktop-notify.ts`, remove that copy
manually when switching to this integration to avoid duplicate notifications.
The installer deliberately leaves it untouched.

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
- Adapts supported window-creation, split, and layout bindings while preserving
  their keys, key tables, repeat flags, and notes.
- Leaves session names unchanged; the sidebar orders enabled workspaces by name.

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

Keep the activation binding in your config so reloading does not remove it.
Version 0.2 removes the old `workmux config` and `workmux configure` commands;
there is no generated config to merge or reload.

### Which bindings are adapted?

The initial implementation supports **single, direct command bodies**:

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

In the sidebar, click a workspace if tmux mouse support is enabled, or focus it
using ordinary pane navigation and use Up/Down or j/k followed by Enter. Escape/q
returns to the remembered content pane. The `>` marker identifies the current workspace.
Workspace selection restores the destination session's selected tab.

### Exiting the last shell

If the last content shell in a tab exits, the sidebar keeps the workspace alive
and displays **No terminals in this tab** instead of an oversized workspace list.
You can still switch workspaces or use your new-tab binding (stock prefix+c).
No shell is respawned automatically.

Creating a foreground tab from this empty view replaces the sidebar-only
placeholder once the new terminal and sidebar are ready. A guarded tmux command
retires only a window containing the sidebar alone; windows with live content
are never removed by this cleanup. Detached native creation (`new-window -d`)
keeps the current empty view and creates a background tab as requested.

`workmux tab new --pane ID` also recovers an empty tab when invoked from another
terminal with the appropriate server/socket. Focus toggling in the empty view is
a no-op, and switching to an empty workspace leaves focus on its picker.

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
workmux tab new editor
workmux split right
workmux split down
workmux layout tiled
workmux layout next
workmux layout balance
workmux sidebar toggle-focus
```

Inside tmux, commands default to its server. Outside tmux, the default server is
named `workmux`. Explicit `--server NAME` or `--socket PATH` overrides inference
and must appear before the subcommand. Pane-oriented commands accept `--pane ID`.
`workspace select` requires an attached client; the sidebar supports one attached
client per session. Run `workmux --help` or a subcommand's help for argument syntax.
`dispatch` is an internal command for adapted keybindings, not a user workflow.

### Optional isolated-server workflow

You can still create workspaces on a separate server, from outside tmux:

```sh
workmux --server workmux-demo workspace new project-a --dir /path/to/project-a --attach
```

This workflow skips the user's startup config and uses internal workmux defaults:
top tabs, mouse support, prefix+Up/Down for workspaces, Left/Right for tabs, c for a
new tab, s for sidebar focus, %/" for splits, and Alt+1–7/Space for layouts. It uses
numbered session names with prefixes hidden in the sidebar. These defaults are
not applied by `enable` to an existing server.

After detaching, remove only the demonstration server with:

```sh
tmux -L workmux-demo kill-server
```

## Runtime and layout limits

- Sidebars are replicated per window and poll every 400 ms.
- The tab bar is tmux's status line, at the user's configured position, spanning
  the whole terminal width. Workmux does not impose a theme on existing sessions.
- Sidebars target 24 columns and shrink in narrow windows; use at least 40 columns.
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

## Develop and test

Use the project-local Nix environment for dependencies and run from the checkout:

```sh
nix-shell
./bin/workmux --help
./scripts/check.sh
```

The checkout launcher puts `bin` on its PATH so new test servers use source code,
not an older installation. Tests use temporary private sockets, clean up servers,
and exercise real pseudoterminal clients. They never alter your running server,
user config, or profile.

Test the installed package from outside the checkout in an isolated pipx environment:

```sh
nix shell nixpkgs#pipx --command ./scripts/check-install.sh
```

## Implementation

- `workmux/bindings.py`: Tree-sitter parsing, supported-operation validation, guarded wrappers.
- `workmux/integration.py`: activation, ownership/reconciliation state, native-operation dispatch.
- `workmux/core.py`: server operations, sidebar/layout helpers, internal isolated-server defaults.
- `workmux/layouts.py`: pure geometry and portable tmux layout serialization.
- `workmux/sidebar.py`: curses workspace picker.
- `workmux/plugins.py`: safe, explicit installation/removal of bundled agent extensions.
- `workmux/resources/pi-notify.ts`: Pi lifecycle notifications and tmux passthrough.
- `workmux/cli.py`: argument parsing, socket inference, error reporting.

This README owns workflows and limitations; command help owns argument syntax.
See the [tmux manual](https://man.openbsd.org/tmux),
[tmux hooks documentation](https://github.com/tmux/tmux/wiki/Events), and
[tmux Tree-sitter grammar](https://github.com/Freed-Wu/tree-sitter-tmux) for underlying behavior.
