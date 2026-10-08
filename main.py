from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import uvicorn  # noqa: E402

from lead_scoring.api import app  # noqa: E402

__all__ = ["app"]

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
