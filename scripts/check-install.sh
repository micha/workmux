#!/usr/bin/env bash
# Exercise the actual wheel through pipx, without touching the user's pipx apps.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if ! command -v pipx >/dev/null; then
  printf 'pipx is required. Try: nix shell nixpkgs#pipx --command ./scripts/check-install.sh\n' >&2
  exit 1
fi
root="$(mktemp -d "${TMPDIR:-/tmp}/workmux-install-test.XXXXXX")"
trap 'rm -rf -- "$root"' EXIT
export PIPX_HOME="$root/home" PIPX_BIN_DIR="$root/bin" PIPX_MAN_DIR="$root/man"
export XDG_CACHE_HOME="$root/cache" PIP_CACHE_DIR="$root/pip-cache"
# The tmux server must resolve portable bindings to this installation, not a global app.
export PATH="$PIPX_BIN_DIR:$PATH"

pipx install --python "$(command -v python3)" "$project_dir[completion]"
cd "$root"
unset PYTHONPATH PYTHONHOME
"$PIPX_BIN_DIR/workmux" --help >/dev/null
python="$PIPX_HOME/venvs/workmux/bin/python"
"$python" -c 'from pathlib import Path; import sys, workmux
path = Path(workmux.__file__).resolve()
assert Path(sys.prefix).resolve() in path.parents, f"Not using installed package: {path}"
print(f"Testing installed package: {path}")'
# Exercise plugin resources through the installed console script, outside checkout.
PI_CODING_AGENT_DIR="$root/pi-agent" "$PIPX_BIN_DIR/workmux" install --agent-dir "$root/pi-agent" | grep -F 'not installed'
test ! -e "$root/pi-agent"
PI_CODING_AGENT_DIR="$root/pi-agent" "$PIPX_BIN_DIR/workmux" install pi
test -f "$root/pi-agent/extensions/workmux-notify.ts"
PI_CODING_AGENT_DIR="$root/pi-agent" "$PIPX_BIN_DIR/workmux" install --agent-dir "$root/pi-agent" | grep -F 'installed'
PI_CODING_AGENT_DIR="$root/pi-agent" "$PIPX_BIN_DIR/workmux" uninstall pi
test ! -e "$root/pi-agent/extensions/workmux-notify.ts"
for harness in opencode codex claude; do
  agent_dir="$root/$harness-agent"
  "$PIPX_BIN_DIR/workmux" install "$harness" --agent-dir "$agent_dir"
  "$PIPX_BIN_DIR/workmux" install --agent-dir "$agent_dir" | grep -E "^[[:space:]]*$harness[[:space:]]+installed[[:space:]]"
  case "$harness" in
    opencode) test -f "$agent_dir/plugins/workmux-notify.js" ;;
    codex) test -f "$agent_dir/hooks.json" ;;
    claude)
      test -f "$agent_dir/skills/workmux/.claude-plugin/plugin.json"
      test -f "$agent_dir/skills/workmux/hooks/hooks.json" ;;
  esac
  "$PIPX_BIN_DIR/workmux" uninstall "$harness" --agent-dir "$agent_dir"
  "$PIPX_BIN_DIR/workmux" install --agent-dir "$agent_dir" | grep -E "^[[:space:]]*$harness[[:space:]]+not installed[[:space:]]"
done
"$python" -m unittest discover -s "$project_dir/tests" -v
