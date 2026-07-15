"""PolyVoice launcher.

Entry point for the bundled Windows EXE and for ``python -m polyvoice.launcher``.

Responsibilities:
  1. On first run (or after a version bump), extract the bundled
     ``node.exe``, ``node_modules/``, ``ffmpeg.exe``, and the bridge
     script from the PyInstaller resource dir into
     ``%APPDATA%/PolyVoice/runtime/``. Subsequent launches skip the
     copy (idempotent), unless the ``runtime/VERSION`` marker is
     missing or stale — that signals an in-place upgrade and forces
     a fresh extraction of the deps.
  2. Start the FastAPI app via uvicorn. The webapp's own
     ``_auto_connect_bridge`` startup hook re-uses the same bridge_runner
     that the user-facing ``/api/whatsapp-web/start`` endpoint uses, so
     the launcher's job is just to bring the server up.
  3. Open the user's default browser to ``http://127.0.0.1:8000`` after
     the server reports it's ready.

Why a separate module (not just a script): PyInstaller freezes the entry
point into ``PolyVoice.exe``; pointing the spec at this file means the
imports needed by the launcher (uvicorn, webbrowser, the polyvoice
package) get pulled into the binary automatically.
"""
from __future__ import annotations

import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

# Make sure the data-root env var is set before anything else in the
# polyvoice package reads paths. ``polyvoice.config`` would also do this,
# but doing it here first means we can resolve paths for our own
# first-run extraction step without depending on a transitive import.
from polyvoice import __version__  # noqa: E402
from polyvoice.logging_setup import configure_logging  # noqa: E402
from polyvoice.paths import data_dir, ensure_data_dir_env, runtime_dir  # noqa: E402

ensure_data_dir_env()


BROWSER_URL = "http://127.0.0.1:8000"
# Seconds to wait after the server binds before opening the browser. Long
# enough for uvicorn to print "Uvicorn running on ..." and the first
# /api/conversations call to succeed, short enough to feel instant.
BROWSER_OPEN_DELAY_SECONDS = 2.5
# Seconds to wait for the server to come up before giving up on the
# browser open. If uvicorn fails to bind (port in use, etc.) the
# launcher should still exit cleanly with a useful message.
SERVER_READY_TIMEOUT_SECONDS = 15.0


def extract_bundled_runtime() -> None:
    """Copy ``node.exe``, ``node_modules/``, ``ffmpeg.exe``, and the bridge
    script out of the bundle.

    Runs only when frozen. On first launch (or after an in-place
    upgrade — detected via the ``runtime/VERSION`` marker), the
    vendored deps are extracted into the per-user data dir.
    Subsequent launches with a matching version find the extracted
    files already in place and only refresh the bridge script (the
    deps stay cached). Errors here are fatal — without Node or
    ffmpeg, the WhatsApp bridge or voice-note pipeline can't run.
    """
    if not getattr(sys, "frozen", False):
        # Dev mode uses `node` from PATH and `ffmpeg` from PATH /
        # POLYVOICE_FFMPEG / WinGet; nothing to extract.
        return

    meipass = Path(getattr(sys, "_MEIPASS"))
    vendor = meipass / "vendor"
    node_exe_src = vendor / "node.exe"
    node_modules_src = vendor / "node_modules"
    bridge_src = meipass / "scripts" / "whatsapp_web_bridge.js"
    ffmpeg_src = vendor / "ffmpeg" / "ffmpeg.exe"
    if not node_exe_src.is_file():
        sys.exit(
            f"[polyvoice] Bundled Node.js not found at {node_exe_src}. "
            f"The PyInstaller bundle may be corrupt."
        )
    if not node_modules_src.is_dir():
        sys.exit(
            f"[polyvoice] Bundled node_modules not found at {node_modules_src}. "
            f"Re-run scripts/build_exe.py to rebuild the bundle."
        )
    if not bridge_src.is_file():
        sys.exit(
            f"[polyvoice] Bundled WhatsApp bridge not found at {bridge_src}. "
            f"The PyInstaller bundle may be corrupt."
        )
    if not ffmpeg_src.is_file():
        sys.exit(
            f"[polyvoice] Bundled ffmpeg not found at {ffmpeg_src}. "
            f"Re-run scripts/build_exe.py to rebuild the bundle."
        )

    target = runtime_dir()
    target_node = target / "node.exe"
    target_modules = target / "node_modules"
    target_bridge = target / "whatsapp_web_bridge.js"
    target_ffmpeg = target / "ffmpeg" / "ffmpeg.exe"
    target_version = target / "VERSION"

    # Always refresh the bridge script — it can change between builds
    # and is small (a few KB). The deps are re-extracted only when the
    # version marker is missing or doesn't match the running build.
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bridge_src, target_bridge)

    needs_extract = _runtime_needs_re_extraction(target_version, target_node, target_modules, target_ffmpeg)

    if not needs_extract:
        # Already extracted from a previous launch. The bridge will reuse
        # them, including any Puppeteer Chromium that was downloaded into
        # node_modules/puppeteer/ at first WhatsApp scan.
        return

    print(f"[polyvoice] Extracting bundled runtime to {target} ...", flush=True)
    # Wipe and re-copy the deps. The bridge script is refreshed above,
    # so we only need to handle the heavy / version-sensitive pieces.
    if target_modules.is_dir():
        shutil.rmtree(target_modules)
    if (target / "ffmpeg").is_dir():
        shutil.rmtree(target / "ffmpeg")
    shutil.copy2(node_exe_src, target_node)
    # ``target_ffmpeg`` lives one directory deeper than node.exe, so the
    # ``ffmpeg/`` parent must exist before the file copy succeeds.
    (target / "ffmpeg").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ffmpeg_src, target_ffmpeg)
    # Use copytree (not copy2) for the directory tree. dirs_exist_ok lets
    # us rerun safely if a previous run got partway through.
    shutil.copytree(node_modules_src, target_modules, dirs_exist_ok=True)
    # Stamp the version so the next launch can skip this work.
    target_version.write_text(__version__ + "\n", encoding="utf-8")
    print(f"[polyvoice] Runtime extracted (version {__version__}).", flush=True)


def _runtime_needs_re_extraction(
    version_file: Path,
    target_node: Path,
    target_modules: Path,
    target_ffmpeg: Path,
) -> bool:
    """Decide whether the launcher must re-extract the vendored runtime.

    Returns True if any of:
      - The version marker is missing.
      - The version marker doesn't match ``__version__`` (in-place upgrade).
      - Any of the vendored files is missing on disk (partial extraction
        from a previous aborted run, or someone wiped the data dir).
    Returns False when the existing extraction is current and complete.
    """
    if not version_file.is_file():
        return True
    try:
        recorded = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    if recorded != __version__:
        return True
    if not (target_node.is_file() and target_modules.is_dir() and target_ffmpeg.is_file()):
        return True
    return False


def open_browser_when_ready() -> None:
    """Wait for the server, then open the browser.

    Runs in a background thread so the main uvicorn loop isn't blocked.
    Uses a simple time-based poll on the /api/conversations endpoint —
    if the server is slow, we just open the browser a few seconds late
    rather than coupling to uvicorn's internals.
    """
    import urllib.request
    import urllib.error

    deadline = time.monotonic() + SERVER_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BROWSER_URL}/api/conversations", timeout=1.0) as response:
                if response.status == 200:
                    break
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.3)

    time.sleep(BROWSER_OPEN_DELAY_SECONDS)
    try:
        webbrowser.open(BROWSER_URL)
    except Exception as exc:  # pragma: no cover - browser launch is best-effort
        print(f"[polyvoice] Could not open browser automatically: {exc!r}", flush=True)
        print(f"[polyvoice] Open {BROWSER_URL} manually.", flush=True)


def main() -> None:
    # PyInstaller-frozen entry points should call freeze_support() before
    # doing anything that spawns child processes (we don't here, but it's
    # cheap insurance if the launcher grows to do so).
    if getattr(sys, "frozen", False):
        # multiprocessing.freeze_support is a no-op when not frozen; gate
        # the import to keep dev startup lean.
        try:
            from multiprocessing import freeze_support
            freeze_support()
        except ImportError:  # pragma: no cover
            pass

    data_dir().mkdir(parents=True, exist_ok=True)
    configure_logging()
    extract_bundled_runtime()

    print(f"[polyvoice] Data directory: {data_dir()}", flush=True)
    print(f"[polyvoice] Starting UI server at {BROWSER_URL}", flush=True)

    # Kick off the browser open in the background. Thread (not asyncio
    # task) because we want it to start before uvicorn.run() blocks the
    # main thread, and threading avoids leaking the loop into the
    # server's own event loop.
    threading.Thread(target=open_browser_when_ready, daemon=True).start()

    try:
        uvicorn.run(
            "polyvoice.webapp:app",
            host="127.0.0.1",
            port=8000,
            log_level="info",
            access_log=False,
        )
    except KeyboardInterrupt:
        # uvicorn handles SIGINT internally; this is just a polite
        # message on the way out.
        print("\n[polyvoice] Shutting down.", flush=True)


if __name__ == "__main__":
    main()
