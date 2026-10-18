# Development guide

- Keep this project independent of neighboring repositories and existing user configuration.
- Runtime code uses tmux, Python's standard library, and Tree-sitter's tmux grammar for evaluated binding parsing. Avoid additional dependencies without a reason.
- Activate existing servers through `enable`; do not reintroduce user-facing config generation/reload commands or overwrite user preferences.
- Keep binding parsing in `workmux/bindings.py` and live reconciliation/dispatch in `workmux/integration.py`. Preserve native fallback behavior in non-enabled sessions.
- Snapshot only owned bindings in tmux's memory; recognize wrappers on re-enable and do not overwrite later user/plugin edits.
- Use `nix-shell --run scripts/check.sh` for validation. Integration tests must use private temporary sockets and clean up servers.
- Use `scripts/check-install.sh` to test packaging in an isolated pipx environment, from outside the checkout.
- `pyproject.toml` owns package metadata and the console entrypoint. Generated helpers must work from installed packages, not depend on `bin/workmux`.
- Exported bindings call `workmux` on tmux's PATH. Reserve explicit Python environment bootstrapping for internal sidebar startup, not user-facing configuration.
- `reinstall-local.sh` is a gitignored Nix convenience script. Test it with `--build-only` or a temporary `--profile`, never the actual user profile.
- Target sessions, windows, and panes by stable IDs, not mutable names or pane indexes.
- Keep terminal rendering in `workmux/sidebar.py`, tmux operations in `workmux/core.py`, and pure geometry in `workmux/layouts.py`.
- Legacy tmux layouts assign panes by list order, not serialized IDs; cover mirrored layouts and pane identity in integration tests.
- Quote shell and tmux configuration arguments separately; test paths containing spaces and shell metacharacters.
- README.md owns user workflows and limitations; argparse help owns command syntax.
- Do not install globally or modify ~/.tmux.conf as part of development; profile installation requires an explicit user action.
