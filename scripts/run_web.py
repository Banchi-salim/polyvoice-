from __future__ import annotations

import sys
from pathlib import Path

import uvicorn


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    uvicorn.run(
        "polyvoice.webapp:app",
        host="127.0.0.1",
        port=8000,
        log_level="info",
        access_log=False,
    )
