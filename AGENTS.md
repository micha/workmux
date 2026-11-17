# Agent development instructions

## Scope and documentation

- Keep workmux independent of neighboring repositories, machine-specific paths,
  existing user configuration, and any particular terminal or package manager.
- `README.md` is for people using workmux: installation, workflows, examples,
  troubleshooting, and user-visible limitations. Keep examples portable and label
  any environment-specific requirements. Do not add workstation procedures,
  agent instructions, or implementation checklists there.
- `AGENTS.md` owns agent development instructions, code organization, validation,
  and optional local tooling. Argparse help owns command syntax.
- Runtime dependencies are tmux, Python's standard library, Tree-sitter, and its
  tmux grammar. Avoid additional dependencies without a reason. Nix is optional
  build/development tooling, never an application requirement.
- `pyproject.toml` owns package metadata, Python dependencies, package data, and
  the console entrypoint.

## Code organization

- `workmux/bindings.py`: parse evaluated bindings with Tree-sitter and its
  [tmux grammar](https://github.com/Freed-Wu/tree-sitter-tmux), validate supported
  operations, and construct guarded wrappers. Parse tmux's normalized `list-keys`
  output, not source configuration files.
- `workmux/integration.py`: live activation, binding ownership/reconciliation,
  and dispatch. Preserve native fallback behavior in non-enabled sessions.
- `workmux/core.py`: tmux operations, sidebar/layout helpers, and internal defaults
  for the optional isolated-server workflow.
- `workmux/layouts.py`: pure geometry and portable tmux layout serialization.
- `workmux/sidebar.py`: terminal rendering and workspace navigation.
- `workmux/status.py`: shared activity symbols and plain animated window-status
  publication. Elect one live owned sidebar from the activity snapshot; batch
  changed window options without changing status formats or refresh intervals.
- `workmux/checkouts.py`: bounded, read-only background discovery of Git checkouts
  and linked worktrees beneath saved workspace directories, including plain
  containers and nested checkouts. Walk directory names within the configured
  depth and existing scan budgets; never follow discovered directory symlinks,
  descend into `.git`, or read checkout file contents.
- `workmux/git_status.py`: optional, local-only Git probes and nonblocking sidebar
  cache. Use saved workspace directories, preserve filenames as opaque porcelain
  records, and never fetch or take optional index locks.
- `workmux/plugins.py`: explicit, safe installation/removal and status reporting
  for bundled agent integrations, including checksum-verified JSON hook files.
- `workmux/agent_hooks.py`: best-effort Codex/Claude command-hook handling. Keep
  hook stdout empty, preserve native decisions/trust, and verify the harness's
  foreground terminal before reporting or sending terminal payloads.
- `workmux/resources/`: bundled Pi/OpenCode lifecycle extensions and Codex/Claude
  hook/plugin JSON. Use native events and root-session identity, not transcripts
  or guesses from tool output.
- `workmux/cli.py`: argument parsing, socket inference, and error reporting.

## Behavioral contracts

- Activate existing servers through `enable`; do not reintroduce user-facing
  config generation/reload commands or overwrite user preferences. Startup hooks
  and activation bindings are user opt-ins, not installed by workmux.
- Snapshot only owned bindings in tmux's memory. Recognize wrappers on re-enable
  and do not overwrite later user/plugin edits or recreate removed bindings.
- Target sessions, windows, and panes by stable IDs, not mutable names or indexes.
- Sidebar focus restoration uses owned, indexed runtime `pane-focus-in` hooks.
  Preserve inherited and local user hooks, do not duplicate entries on re-enable
  or recreate edited/removed entries, and leave `focus-events` unchanged. Return
  focus natively only from an active owned sidebar with other panes; preserve
  mouse click delivery and the actual previous content pane.
- Sidebar reload respawns only owned panes in managed sessions, in place, using
  the invoking installation. Recheck ownership in tmux before respawning; preserve
  content, pane IDs, layout, focus, and zoom. Do not recreate missing sidebars.
- Close tabs when only their owned sidebar remains; closing the final tab ends
  the workspace. Recheck ownership and pane count in tmux before destruction,
  preserve live content, and honor native lifecycle settings.
- Legacy tmux layouts assign panes by list order, not serialized IDs. Cover
  mirrored layouts and pane identity in integration tests.
- Quote shell and tmux configuration arguments separately. Test paths containing
  spaces and shell metacharacters.
- Exported bindings call `workmux` on tmux's PATH. Reserve explicit Python
  environment bootstrapping for internal sidebar startup, not user-facing config.
- Helpers and bundled resources must work from installed packages; do not depend
  on `bin/workmux` or the source checkout. Include agent resources in both Python
  package data and any optional build tooling's source filters.
- Plugin commands manage only their owned files. Preserve edited files, symlinks,
  unrelated extensions, and user settings. Status listing must be read-only.
- Agent state is explicit pane-local metadata, not inferred from command names.
  Aggregate reports and native alerts in a batched snapshot; never resolve every
  pane with separate subprocesses on each sidebar refresh. Ignore stale process
  IDs and dead panes. Attention/finished states classify pending native alerts;
  hide them when alerts are cleared, while working stays visible independently.
  Preserve tmux's native alert monitoring/clearing behavior.
- Checkout rows use stable session IDs and checkout paths; associated tabs use
  explicit window metadata, never shell cwd or branch names. Open tabs lazily,
  revalidate containment before opening, and never destroy content on checkout removal.
  Render checkouts and agent panes as direct workspace children, independent of
  reported roots. Checkout labels show only the directory basename; preserve full
  paths in row identities and click targets, including duplicate basenames.
  Keep window-status priority aggregation separate.
- Tree rows and click targets use stable session/window IDs, not rendered indexes
  or names. Prefer attention over working over finished when agents share a tab.
- Pi state reports must be serialized and best-effort so a missing workmux command
  or closed pane does not block the agent. Use blocking UI prompt and final
  settlement events, not low-level run-end events. Sanitize terminal payloads;
  neither report state nor emit escapes in non-interactive/RPC modes.

## Development and validation

Use Python 3.10+ with `curses`, tmux 3.3+, and the declared Python dependencies.
A standard virtual environment is sufficient; Nix is not required:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
./bin/workmux --help
./scripts/check.sh
```

- `scripts/check.sh` runs unittest discovery and puts the checkout launcher on
  PATH so private test servers execute source code rather than an older install.
- Integration tests must use private temporary sockets and clean up servers.
  Tests must not alter running user servers, configuration, or profiles.
- Run `scripts/check-install.sh` with pipx available to validate packaging. It
  builds an isolated pipx installation, runs from outside the checkout, and tests
  plugin installation with temporary agent directories. Do not replace this with
  testing only from the source tree.
- The TypeScript notification tests use Node 24+ for native TypeScript loading.
  Without a suitable Node version they are skipped; check for skips when changing
  the extension. Node is not a workmux runtime dependency.
- Optional native harness tests use temporary HOME/config/data/cache directories
  and make no model calls. Validate plugin discovery, schemas, and native hook
  trust without approving hooks or changing real harness settings.
- Integration coverage has been exercised with tmux 3.6a and 3.8 on Linux; the
  pinned flake has been built and tested on `aarch64-linux`. Do not imply other
  tmux versions, platforms, or terminal notification implementations have been
  validated without testing them.
- Check `git diff --check` before finishing. Update human-facing documentation
  when workflows or limitations change.

### README screenshot

- `scripts/demo.py` stages disposable Git repositories and simulated agent/test
  output, then captures the real sidebar and panes with VHS. It makes no model
  calls, uses a private temporary tmux socket and HOME, and cleans up its server.
- Recreate `docs/demo.png` with
  `nix develop .#demo --command python scripts/demo.py`. Use `--output PATH` for
  previews. Outside Nix, supply the Python dependencies, tmux, Git, Bash, VHS,
  ttyd, FFmpeg, Chromium, and DejaVu Sans Mono; configure VHS's browser/font paths
  as needed. These tools are optional development dependencies, not runtime ones.
- The Nix demo shell disables Chromium's sandbox only for VHS's local renderer
  because the Nix sandbox helper is not configured on every host. Do not use that
  setting for untrusted pages. Review the PNG before updating the README.

### Optional Nix tooling

- `flake.nix` exposes the application, development shell, and checks for
  `x86_64-linux` and `aarch64-linux`; `flake.lock` pins Nixpkgs. Validate with
  `nix build`, `nix flake check`, `nix develop --command scripts/check.sh`, and
  `nix develop --command scripts/check-install.sh`. Keep the lock file tracked;
  update it deliberately with `nix flake update nixpkgs` and rerun validation.
- `shell.nix` shares the development shell with the flake: Python dependencies,
  tmux, pipx, Git, and Node 24+ for notification tests. The legacy
  `nix-shell --run scripts/check.sh` and `nix-shell --run scripts/check-install.sh`
  remain available, but use the machine's `<nixpkgs>` rather than the lock file.
- `nix/workmux.nix` defines the optional application build, includes bundled agent
  resources, and smoke-tests install/uninstall in a temporary agent directory.
  Pin private executables without changing the caller's PATH; keep Python module
  bootstrapping process-local. `nix/check-environment.py` checks the installed
  launcher, sidebars, and content-shell environments using a private tmux socket.
  `nix/python-deps.nix` defines the grammar dependency. `nix/checks.nix` runs the
  suite against the installed Nix package outside the checkout, without network
  downloads. Keep the separate pipx check: it validates the non-Nix installation.
- Validate profile installation only with an explicit temporary `--profile`;
  never run `nix profile add .` against the user's real profile during development.
  Local Git flakes see only tracked files, so add new source files to the index
  before validating the flake.
- Some workstations have a gitignored `reinstall-local.sh` convenience script.
  Do not assume it exists in a fresh clone or add it to distribution docs. When
  present, it delegates to `nix/workmux.nix`; test changes with `--build-only` or a
  temporary `--profile`, never the actual user profile.

## Safety

Do not install globally, modify user tmux/Pi configuration, or replace a user
profile as part of development. Profile installation and real plugin installation
require an explicit user action; validation must use isolated temporary targets.
