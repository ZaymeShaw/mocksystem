"""Compatibility entry point; use python -m eval_harness.suite."""
from .suite import main

if __name__ == "__main__":
    raise SystemExit(main())
