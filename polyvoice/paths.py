"""Centralized path resolution for PolyVoice.

All runtime data (conversations, reply audio, voice profiles, WhatsApp session,
generated logs) lives under a single root. The launcher overrides this root
via the ``POLYVOICE_DATA_DIR`` environment variable so the bundled Windows
EXE can write to ``%APPDATA%\\PolyVoice\\`` while dev mode (``python -m
polyvoice.launcher``) keeps the historical ``./data`` layout.

Module-level path objects (``WHATSAPP_SESSION_DIR`` etc.) are kept here as
functions rather than constants so the values are computed at call time and
respect env-var changes between frozen and dev modes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _default_data_dir() -> Path:
    """Pick the default data root for the current environment.

    Frozen Windows EXE → ``%APPDATA%\\PolyVoice``. Anywhere else (dev
    ``python -m``) → ``./data`` next to the project root, matching the
    pre-EXE layout so existing dev workflows keep working.
    """
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "PolyVoice"
    return Path("data")


def ensure_data_dir_env() -> Path:
    """Set ``POLYVOICE_DATA_DIR`` to the appropriate default if unset.

    Idempotent. Returns the resolved data dir. Callers (the launcher,
    the config module on import) should run this before any other
    ``polyvoice.paths.*()`` helper so the env var is stable for the
    lifetime of the process.
    """
    os.environ.setdefault("POLYVOICE_DATA_DIR", str(_default_data_dir()))
    return Path(os.environ["POLYVOICE_DATA_DIR"])


def data_dir() -> Path:
    """Root directory for all PolyVoice runtime data."""
    return Path(os.environ.get("POLYVOICE_DATA_DIR", str(_default_data_dir())))


def runtime_dir() -> Path:
    """Directory holding the extracted Node.js + ffmpeg runtimes and the
    bridge deps.

    The launcher copies ``node.exe``, ``node_modules/``, ``ffmpeg.exe``,
    and the bridge script from the bundled EXE into this directory on
    first run (or after a version bump). Subsequent launches reuse it.
    """
    return data_dir() / "runtime"


def node_exe_path() -> Path:
    """Absolute path to the Node.js executable the launcher should spawn."""
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return runtime_dir() / "node.exe"
    # Dev mode: assume `node` is on PATH.
    return Path("node")


def ffmpeg_path() -> Path:
    """Absolute path to the ffmpeg executable the audio pipeline shells out to.

    When frozen, the launcher extracts the bundled ``ffmpeg.exe`` from
    ``_MEIPASS/vendor/ffmpeg/`` into ``<data_dir>/runtime/ffmpeg/`` on first
    launch, so the EXE ships self-contained — no system ffmpeg install
    required. In dev mode, fall back to a bare ``ffmpeg`` and let the
    PATH / ``POLYVOICE_FFMPEG`` / WinGet fallback chain in
    ``polyvoice.audio.locate_ffmpeg`` resolve it.
    """
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return runtime_dir() / "ffmpeg" / "ffmpeg.exe"
    return Path("ffmpeg")


def bridge_entry() -> Path:
    """Path to the WhatsApp Web bridge script.

    When frozen, the launcher copies the bridge source into the extracted
    runtime directory next to ``node.exe`` and ``node_modules``. In dev, it's
    at the project root.
    """
    if getattr(sys, "frozen", False):
        return runtime_dir() / "whatsapp_web_bridge.js"
    return Path(__file__).resolve().parents[1] / "scripts" / "whatsapp_web_bridge.js"


def conversations_path() -> Path:
    """JSONL file holding the conversation history."""
    return data_dir() / "conversations.jsonl"


def reply_audio_dir() -> Path:
    """Directory for generated OGG voice-note replies."""
    return data_dir() / "reply_audio"


def log_file_path() -> Path:
    """Main application log file."""
    return data_dir() / "polyvoice.log"


def voice_profiles_dir() -> Path:
    """Directory for user voice samples and the local voice profile manifest."""
    return data_dir() / "voice_profiles"


def whatsapp_session_dir() -> Path:
    """Directory the Node bridge writes its ``LocalAuth`` session to.

    Mirrors the structure under ``data/`` that the bridge expects: the
    ``clientId``-suffixed subdirectory ``session-polyvoice/`` is created by
    whatsapp-web.js itself; we only own the parent.
    """
    return data_dir() / "whatsapp-web-session" / "session-polyvoice"


def env_file_path() -> Path | None:
    """Path to the user's ``.env`` file, or ``None`` if not configured.

    Order of preference:
      1. ``<data_dir>/.env`` (APPDATA, where the EXE keeps user state).
      2. ``<exe_dir>/.env`` (sits next to PolyVoice.exe — convenient for
         portable installs where the user wants the file alongside the
         binary).
    """
    candidates = [data_dir() / ".env"]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / ".env")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
