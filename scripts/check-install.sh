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

pipx install --python "$(command -v python3)" "$project_dir"
cd "$root"
unset PYTHONPATH PYTHONHOME
"$PIPX_BIN_DIR/workmux" --help >/dev/null
python="$PIPX_HOME/venvs/workmux/bin/python"
"$python" -c 'from pathlib import Path; import sys, workmux
path = Path(workmux.__file__).resolve()
assert Path(sys.prefix).resolve() in path.parents, f"Not using installed package: {path}"
print(f"Testing installed package: {path}")'
"$python" -m unittest discover -s "$project_dir/tests" -v
