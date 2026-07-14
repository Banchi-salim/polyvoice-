# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the PolyVoice Windows EXE.

Build sequence (run from the project root):
  1. python scripts/build_exe.py          # stage node.exe + node_modules + ffmpeg.exe
  2. pyinstaller --noconfirm PolyVoice.spec

Output: dist/PolyVoice/PolyVoice.exe + dist/PolyVoice/_internal/
(distributable as a single folder / zip).

What this spec ships inside the EXE (under _MEIPASS/):
  - polyvoice/templates/         (Jinja templates)
  - polyvoice/static/            (CSS / JS assets)
  - scripts/whatsapp_web_bridge.js
  - scripts/package.json         (the bridge manifest)
  - vendor/node.exe              (portable Node.js)
  - vendor/node_modules/         (bridge deps, no Chromium)
  - vendor/ffmpeg/ffmpeg.exe     (bundled for OGG/Opus voice-note transcode)

What it deliberately does NOT ship:
  - data/                        (runtime data, lives in %APPDATA%)
  - node_modules/                (the dev top-level node_modules)
  - .wwebjs_cache/
  - .env                         (user secrets)
  - tests/                       (not part of the product)
  - .idea/, .claude/             (dev tooling)
  - __pycache__/
  - scripts/data/

The launcher is responsible for first-run extraction of vendor/* from
_MEIPASS into %APPDATA%/PolyVoice/runtime/. See polyvoice/launcher.py.
"""
import os
import shutil
import sys
from pathlib import Path

block_cipher = None

# SPECPATH is set by PyInstaller to the directory containing the .spec.
PROJECT = Path(os.path.abspath(SPECPATH))
VENDOR = PROJECT / "build" / "vendor"

# Required inputs. The spec aborts early if the build helper didn't run
# — running the EXE without these would just fail at first launch with a
# confusing error.
REQUIRED = [
    VENDOR / "node.exe",
    VENDOR / "node_modules",
    VENDOR / "ffmpeg" / "ffmpeg.exe",
    PROJECT / "scripts" / "whatsapp_web_bridge.js",
    PROJECT / "scripts" / "bridge" / "package.json",
]
for required in REQUIRED:
    if not required.exists():
        sys.exit(
            f"[PolyVoice.spec] missing required asset: {required}\n"
            f"Run `python scripts/build_exe.py` first to stage vendored deps."
        )

# Data files that ship inside the EXE. The destination paths are relative
# to the bundle's _MEIPASS root.
datas = [
    # App templates & static — read at request time by FastAPI.
    (str(PROJECT / "polyvoice" / "templates"), "polyvoice/templates"),
    (str(PROJECT / "polyvoice" / "static"), "polyvoice/static"),
    # Bridge source — extracted to the runtime dir by the launcher.
    (str(PROJECT / "scripts" / "whatsapp_web_bridge.js"), "scripts"),
    (str(PROJECT / "scripts" / "bridge" / "package.json"), "scripts"),
    # Vendored Node.js runtime.
    (str(VENDOR / "node.exe"), "vendor"),
    (str(VENDOR / "node_modules"), "vendor/node_modules"),
    # Bundled ffmpeg for the WhatsApp voice-note OGG/Opus transcode.
    # Without this the end user has to install ffmpeg separately, which
    # contradicts the "no manual setup" goal.
    (str(VENDOR / "ffmpeg" / "ffmpeg.exe"), "vendor/ffmpeg"),
]

# Modules we never need at runtime. Listing them keeps the EXE smaller
# and shortens the build's static analysis pass.
excludes = [
    "data", "node_modules", ".wwebjs_cache", "scripts/data",
    "tests", ".idea", ".claude", "__pycache__",
    # Heavy scientific / GUI stacks we don't use.
    "tkinter", "turtle", "unittest", "pydoc", "doctest",
    "matplotlib", "numpy", "pandas", "scipy",
    "PyInstaller", "pyinstaller_hooks_contrib",
]

# Hidden imports — modules PyInstaller's static analysis misses because
# they are loaded via string imports, factory functions, or third-party
# plugins. polyvoice.providers is the main offender.
hiddenimports = [
    "polyvoice.webapp", "polyvoice.config", "polyvoice.paths",
    "polyvoice.bridge_runner",
    "polyvoice.conversations", "polyvoice.whatsapp",
    "polyvoice.voice", "polyvoice.audio", "polyvoice.orchestrator",
    "polyvoice.providers", "polyvoice.providers.factory",
    "polyvoice.providers.mock", "polyvoice.providers.base",
    "polyvoice.providers.deepgram", "polyvoice.providers.whisper_stt",
    "polyvoice.providers.google_translate",
    "polyvoice.providers.groq_conversation",
    "polyvoice.providers.elevenlabs_tts",
    # uvicorn's protocol/loop modules are loaded by string at runtime.
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    # jinja2 extensions and python-multipart.
    "jinja2.ext", "multipart",
]

a = Analysis(
    [str(PROJECT / "polyvoice" / "launcher.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,        # one-dir: keep _internal/ separate
    name="PolyVoice",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # disable UPX — antivirus + faster startup
    console=True,                 # user picked "console window shows logs"
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                    # add a .ico path here for a branded build
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PolyVoice",
)

# Post-build: write README.txt next to the EXE.
readme_src = PROJECT / "dist" / "PolyVoice.README.txt"
readme_dst = Path(coll.name) / "README.txt"
if readme_src.is_file():
    shutil.copy(readme_src, readme_dst)
else:
    print(
        f"[PolyVoice.spec] (info) No {readme_src} found — "
        f"skipping the user-facing README next to the EXE."
    )
