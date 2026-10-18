#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
# Exercise bindings against the checkout, not an older installed workmux.
export PATH="$PWD/bin:$PATH"
python3 -m unittest discover -s tests -v
