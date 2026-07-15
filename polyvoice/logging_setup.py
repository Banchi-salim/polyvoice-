from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TextIO

from polyvoice.paths import log_file_path

_CONFIGURED = False


class _Tee:
    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self.primary = primary
        self.secondary = secondary

    def write(self, value: str) -> int:
        self.primary.write(value)
        self.secondary.write(value)
        return len(value)

    def flush(self) -> None:
        self.primary.flush()
        self.secondary.flush()

    def isatty(self) -> bool:
        return bool(getattr(self.primary, "isatty", lambda: False)())


def configure_logging() -> Path:
    """Send stdout, stderr, and Python logging to the PolyVoice log file."""
    global _CONFIGURED

    path = log_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if _CONFIGURED:
        return path

    log_stream = path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.stdout, log_stream)  # type: ignore[assignment]
    sys.stderr = _Tee(sys.stderr, log_stream)  # type: ignore[assignment]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(path, encoding="utf-8"),
            logging.StreamHandler(sys.__stderr__),
        ],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.captureWarnings(True)
    _CONFIGURED = True
    print(f"[polyvoice] Logging to {path}", flush=True)
    return path
