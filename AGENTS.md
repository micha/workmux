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
- `workmux/plugins.py`: explicit, safe installation/removal and status reporting
  for bundled agent extensions.
- `workmux/resources/pi-notify.ts`: Pi lifecycle state reports, notifications,
  and tmux passthrough.
- `workmux/cli.py`: argument parsing, socket inference, and error reporting.

## Behavioral contracts

- Activate existing servers through `enable`; do not reintroduce user-facing
  config generation/reload commands or overwrite user preferences. Startup hooks
  and activation bindings are user opt-ins, not installed by workmux.
- Snapshot only owned bindings in tmux's memory. Recognize wrappers on re-enable
  and do not overwrite later user/plugin edits or recreate removed bindings.
- Target sessions, windows, and panes by stable IDs, not mutable names or indexes.
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
- Integration coverage has been exercised with tmux 3.6a on Linux. Do not imply
  other tmux versions, platforms, or terminal notification implementations have
  been validated without testing them.
- Check `git diff --check` before finishing. Update human-facing documentation
  when workflows or limitations change.

### Optional Nix tooling

- `shell.nix` supplies the Python dependencies, tmux, and pipx. When available,
  validate with `nix-shell --run scripts/check.sh` and
  `nix-shell --run scripts/check-install.sh`.
- `nix/workmux.nix` defines the optional application build, includes bundled Pi
  resources, and smoke-tests install/uninstall in a temporary agent directory.
  `nix/python-deps.nix` defines the grammar dependency.
- Some workstations have a gitignored `reinstall-local.sh` convenience script.
  Do not assume it exists in a fresh clone or add it to distribution docs. When
  present, it delegates to `nix/workmux.nix`; test changes with `--build-only` or a
  temporary `--profile`, never the actual user profile.

## Safety

Do not install globally, modify user tmux/Pi configuration, or replace a user
profile as part of development. Profile installation and real plugin installation
require an explicit user action; validation must use isolated temporary targets.
