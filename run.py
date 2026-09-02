#!/usr/bin/env python3
"""Convenience launcher so you don't need PYTHONPATH set.

    python3 run.py probe
    python3 run.py once
    python3 run.py run
    python3 run.py guard-check
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rbh_hedge_var.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
