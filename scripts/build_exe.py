"""Build helper: stage the vendored Node.js + ffmpeg runtimes and the
bridge dependencies.

Run from the project root before invoking ``pyinstaller PolyVoice.spec``:

    python scripts/build_exe.py

What it does:
  1. ``mkdir build/vendor``
  2. Copy the system ``node.exe`` (the same Node the dev machine uses) into
     ``build/vendor/node.exe`` so the bundled EXE doesn't depend on the
     user's PATH at runtime.
  3. Copy the system ``ffmpeg.exe`` (or fall back to the WinGet install)
     into ``build/vendor/ffmpeg/ffmpeg.exe`` so the bundled EXE can
     transcode TTS replies to the OGG/Opus format WhatsApp PTT requires
     without the user installing ffmpeg separately.
  4. Run ``npm ci --omit=dev --prefix scripts/bridge`` to populate
     ``scripts/bridge/node_modules/`` with only the bridge's runtime deps
     (no Express typings, no Puppeteer Chromium download, etc.).
  5. Copy ``scripts/bridge/node_modules/`` to ``build/vendor/node_modules/``
     so PyInstaller's spec can include it as a data file.

The output (``build/vendor/``) is then referenced by ``PolyVoice.spec``.
After the EXE is built, ``build/vendor/`` can be deleted — it's not used
at runtime, only at build time.

Why a separate ``scripts/bridge/`` subpackage: the top-level ``package.json``
in this project is the dev manifest (used by ``npm install`` for local
WhatsApp Web development). The bridge subpackage has a tighter
``npm ci --omit=dev`` rule that keeps the EXE's vendored deps minimal.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = PROJECT_ROOT / "build" / "vendor"
BRIDGE_DIR = PROJECT_ROOT / "scripts" / "bridge"
NODE_MODULES_SRC = BRIDGE_DIR / "node_modules"
NODE_MODULES_DST = VENDOR_DIR / "node_modules"
FFMPEG_DST = VENDOR_DIR / "ffmpeg" / "ffmpeg.exe"

# Known WinGet install location for the Gyan.FFmpeg package. This is the
# same path ``polyvoice.audio.locate_ffmpeg`` already probes as a dev
# convenience — keep them in sync.
_WINGET_FFMPEG = Path(
    r"C:\Users\salim\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"
)


def _locate_system_node() -> Path:
    """Find ``node.exe`` on the build machine. Prefer the system install."""
    candidates = [
        Path(r"C:\Program Files\nodejs\node.exe"),
        Path(r"C:\Program Files (x86)\nodejs\node.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Fall back to ``where node`` on Windows.
    try:
        result = subprocess.run(
            ["where", "node"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        sys.exit(
            f"[build_exe] Could not find node.exe on the build machine.\n"
            f"Install Node.js 18+ from https://nodejs.org and try again.\n"
            f"Underlying error: {exc!r}"
        )
    first_match = result.stdout.splitlines()[0].strip()
    return Path(first_match)


def stage_node() -> None:
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    src = _locate_system_node()
    dst = VENDOR_DIR / "node.exe"
    print(f"[build_exe] Copying {src} -> {dst}")
    shutil.copy2(src, dst)


def _locate_system_ffmpeg() -> Path:
    """Find ``ffmpeg.exe`` on the build machine.

    Order: known WinGet path, ``POLYVOICE_FFMPEG`` env var override,
    ``where ffmpeg``. Aborts with a clear install message if none of
    these resolve — building without bundling ffmpeg would force every
    end user to install ffmpeg manually, which contradicts the
    no-manual-setup goal.
    """
    if _WINGET_FFMPEG.is_file():
        return _WINGET_FFMPEG
    env_override = os.environ.get("POLYVOICE_FFMPEG")
    if env_override and Path(env_override).is_file():
        return Path(env_override)
    try:
        result = subprocess.run(
            ["where", "ffmpeg"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        result = None
    if result is not None:
        for line in result.stdout.splitlines():
            candidate = line.strip()
            if candidate and candidate.lower().endswith("ffmpeg.exe"):
                return Path(candidate)
    sys.exit(
        f"[build_exe] Could not find ffmpeg.exe on the build machine.\n"
        f"Install ffmpeg (e.g. `winget install Gyan.FFmpeg`) and try again,\n"
        f"or set the POLYVOICE_FFMPEG env var to the absolute path of\n"
        f"ffmpeg.exe so the build helper can stage it into the bundle."
    )


def stage_ffmpeg() -> None:
    """Copy ffmpeg.exe into ``build/vendor/ffmpeg/`` for the bundle.

    The destination path is what ``PolyVoice.spec`` adds to ``datas=``;
    if it isn't staged the spec aborts the build with a clear error.
    """
    FFMPEG_DST.parent.mkdir(parents=True, exist_ok=True)
    src = _locate_system_ffmpeg()
    print(f"[build_exe] Copying {src} -> {FFMPEG_DST}")
    shutil.copy2(src, FFMPEG_DST)


def _locate_npm() -> str:
    """Find the npm binary on the build machine.

    On Windows, ``npm`` is a ``.cmd`` shim. ``where npm`` returns both
    the bare ``npm`` (a non-executable shim) and the real ``npm.cmd``;
    we want the ``.cmd`` to avoid ``OSError: [WinError 193]`` when
    subprocess tries to launch the bare name. The shim is still a
    real program — it just needs to be invoked through the cmd
    interpreter, which is what its extension tells subprocess.
    """
    try:
        result = subprocess.run(
            ["where", "npm"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        sys.exit(
            f"[build_exe] Could not find npm on the build machine.\n"
            f"Install Node.js 18+ (which bundles npm) from "
            f"https://nodejs.org and try again.\n"
            f"Underlying error: {exc!r}"
        )
    # Prefer .cmd, then .exe, then .bat. Skip bare names — those are
    # Windows shims that aren't directly executable.
    candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for ext in (".cmd", ".exe", ".bat"):
        for candidate in candidates:
            if candidate.lower().endswith(ext):
                return candidate
    sys.exit(
        f"[build_exe] `where npm` did not return a .cmd/.exe/.bat binary.\n"
        f"Output was:\n{result.stdout}\n"
        f"Install Node.js 18+ from https://nodejs.org and try again."
    )


def stage_bridge_deps(npm_path: str) -> None:
    """Install the bridge's runtime deps into scripts/bridge/node_modules/.

    Prefers ``npm ci`` (reproducible, fast) when a lock file is present;
    falls back to ``npm install`` for the first build. Either way the
    resulting tree is copied into ``build/vendor/node_modules/`` for the
    PyInstaller step to bundle.
    """
    if not (BRIDGE_DIR / "package.json").is_file():
        sys.exit(
            f"[build_exe] Missing {BRIDGE_DIR / 'package.json'}. "
            f"The bridge subpackage manifest is required for the EXE build."
        )

    has_lock = (BRIDGE_DIR / "package-lock.json").is_file()
    sub = ["ci", "--omit=dev"] if has_lock else ["install", "--omit=dev"]
    cmd = [npm_path, *sub]
    label = "ci" if has_lock else "install"
    print(f"[build_exe] Installing bridge dependencies in {BRIDGE_DIR} (npm {label}) ...")
    result = subprocess.run(cmd, cwd=str(BRIDGE_DIR))
    if result.returncode != 0:
        sys.exit(
            f"[build_exe] npm {label} failed (exit {result.returncode}). "
            f"Check the error log above and your network connection."
        )
    if not NODE_MODULES_SRC.is_dir():
        sys.exit(
            f"[build_exe] npm {label} did not produce {NODE_MODULES_SRC}. "
            f"Check the npm output and your network connection."
        )

    print(f"[build_exe] Copying {NODE_MODULES_SRC} -> {NODE_MODULES_DST}")
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    if NODE_MODULES_DST.exists():
        shutil.rmtree(NODE_MODULES_DST)
    shutil.copytree(NODE_MODULES_SRC, NODE_MODULES_DST)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--skip-npm",
        action="store_true",
        help="Skip the npm install step. Use when the bridge's node_modules is already populated.",
    )
    parser.add_argument(
        "--skip-ffmpeg",
        action="store_true",
        help="Skip the ffmpeg staging step. Use when build/vendor/ffmpeg/ffmpeg.exe is already in place.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stage_node()
    if not args.skip_ffmpeg:
        stage_ffmpeg()
    if not args.skip_npm:
        npm_path = _locate_npm()
        stage_bridge_deps(npm_path)
    print(f"[build_exe] Vendored runtime ready in {VENDOR_DIR}")


if __name__ == "__main__":
    main()
