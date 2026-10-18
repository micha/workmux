"""Support python -m workmux, including installed sidebar subprocesses."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
