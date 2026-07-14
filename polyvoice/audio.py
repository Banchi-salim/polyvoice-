"""Audio helpers — ffmpeg-based transcoding for WhatsApp voice notes.

WhatsApp voice notes (PTT, push-to-talk bubbles with waveform) require the
audio to be encoded as Opus in an OGG container, 16 kHz mono, around 32 kbps.
The ElevenLabs provider returns MP3 by default (or raw PCM if configured),
neither of which WhatsApp's PTT uploader accepts. This module wraps ffmpeg to
transcode either source into the format WhatsApp expects.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

from polyvoice.paths import ffmpeg_path

# Canonical voice-note mime type. The `; codecs=opus` parameter tells
# WhatsApp's PTT uploader to treat the upload as Opus audio specifically.
VOICE_NOTE_MIME_TYPE = "audio/ogg; codecs=opus"

# Canonical filename. WhatsApp Web shows this name on the attachment; the
# extension must match the container or the client refuses to render the
# PTT bubble.
VOICE_NOTE_FILENAME = "polyvoice-reply.ogg"

# Fallback for the well-known WinGet install of ffmpeg on this machine. PATH
# sometimes does not include user-local apps on Windows, so we check the
# canonical install path explicitly before declaring ffmpeg missing.
_WINGET_FFMPEG_PATH = Path(
    r"C:\Users\salim\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"
)


def locate_ffmpeg() -> str:
    """Find the ffmpeg executable. Returns absolute path.

    Order of resolution:
    1. The bundled ``runtime/ffmpeg/ffmpeg.exe`` extracted by the launcher
       when running the PyInstaller EXE — this is the self-contained path
       the end user hits.
    2. The `POLYVOICE_FFMPEG` env var (lets users override the location).
    3. `shutil.which("ffmpeg")` (standard PATH lookup).
    4. The canonical WinGet install path on this machine (dev convenience).
    Raises RuntimeError with a clear install message if none of these work.
    """
    bundled = ffmpeg_path()
    if bundled.is_file():
        return str(bundled)
    override = os.environ.get("POLYVOICE_FFMPEG")
    if override and Path(override).is_file():
        return override
    found = shutil.which("ffmpeg")
    if found:
        return found
    if _WINGET_FFMPEG_PATH.is_file():
        return str(_WINGET_FFMPEG_PATH)
    raise RuntimeError(
        "ffmpeg is required to encode WhatsApp voice notes. "
        "Install it (e.g. `winget install Gyan.FFmpeg`) or set POLYVOICE_FFMPEG "
        "to the path of the executable."
    )


def transcode_to_ogg_opus(
    audio: bytes,
    source_mime: str,
    source_sample_rate: int | None = None,
) -> bytes:
    """Transcode `audio` into OGG/Opus at 16 kHz mono, ~32 kbps, voip profile.

    Parameters
    ----------
    audio : bytes
        The raw audio bytes from the TTS provider.
    source_mime : str
        The mime type of the source bytes. Supported: `audio/mpeg` (MP3),
        `audio/mp3`, `audio/pcm` (raw signed 16-bit little-endian mono).
    source_sample_rate : int, optional
        Required when `source_mime` is `audio/pcm` — ffmpeg needs to know
        the sample rate to interpret raw PCM. Ignored for compressed inputs.

    Returns
    -------
    bytes
        The full OGG/Opus payload. 16 kHz mono, ~32 kbps, with `-application
        voip` so the encoder optimises for speech intelligibility.
    """
    ffmpeg = locate_ffmpeg()
    # Build the input side of the command. Compressed formats are autodetected
    # from the stream; PCM needs explicit -f/-ar/-ac so ffmpeg doesn't try to
    # treat raw bytes as an OGG/MP3 header.
    if source_mime in {"audio/mpeg", "audio/mp3"}:
        input_args = ["-i", "pipe:0"]
    elif source_mime == "audio/pcm":
        if not source_sample_rate:
            raise ValueError("source_sample_rate is required when source_mime is 'audio/pcm'.")
        input_args = [
            "-f", "s16le",
            "-ar", str(source_sample_rate),
            "-ac", "1",
            "-i", "pipe:0",
        ]
    else:
        # Let ffmpeg guess from the bytes — best-effort for WAV/Ogg/etc.
        input_args = ["-i", "pipe:0"]

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        *input_args,
        "-c:a", "libopus",
        "-ar", "16000",
        "-ac", "1",
        "-b:a", "32k",
        "-application", "voip",
        "-vbr", "on",
        "-compression_level", "10",
        "-f", "ogg",
        "pipe:1",
    ]
    completed = subprocess.run(
        command,
        input=audio,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"ffmpeg failed to transcode {source_mime} to OGG/Opus "
            f"(exit {completed.returncode}): {stderr or '<no stderr>'}"
        )
    return completed.stdout


async def transcode_to_ogg_opus_async(
    audio: bytes,
    source_mime: str,
    source_sample_rate: int | None = None,
) -> bytes:
    """Async wrapper around :func:`transcode_to_ogg_opus`.

    The synchronous version blocks the event loop while ffmpeg runs (typically
    tens of milliseconds for a short voice-note reply). We delegate to a
    thread so the FastAPI request handler stays responsive under load.
    """
    return await asyncio.to_thread(transcode_to_ogg_opus, audio, source_mime, source_sample_rate)
