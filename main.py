"""Compatibility entry point for local development from a source checkout."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from job_scout.cli import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run())
