from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Make sure POLYVOICE_DATA_DIR has a sensible value *before* anything else
# (including the helper that reads it) tries to resolve a path. We do this
# here so the rest of the package can call polyvoice.paths.*() without
# worrying about import order.
from polyvoice.paths import ensure_data_dir_env, env_file_path  # noqa: E402

ensure_data_dir_env()

# Load .env from a known path. Calling load_dotenv() with no args uses CWD,
# which is unreliable when the bundled EXE is launched from a Windows
# shortcut or a different working directory — so we prefer the explicit
# resolved path, and fall back to dotenv's CWD-relative discovery for
# dev mode where the .env sits at the project root.
_env_file = env_file_path()
if _env_file is not None:
    load_dotenv(_env_file, override=False)
else:
    # Dev convenience: pick up the .env next to the project root.
    load_dotenv(override=False)


@dataclass(frozen=True)
class PolyVoiceConfig:
    mode: str = "Real"
    default_source_language: str = "en"
    default_target_language: str = "ig"
    max_response_words: int = 15
    latency_target_ms: float = 200.0

    # STT providers
    stt_provider: str = "deepgram"  # "deepgram" or "whisper"
    deepgram_api_key: str | None = None
    openai_api_key: str | None = None  # For Whisper

    # Other providers
    google_translate_api_key: str | None = None
    google_application_credentials: str | None = None
    groq_api_key: str | None = None
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_verify_token: str | None = None
    whatsapp_api_version: str = "v20.0"


def load_config() -> PolyVoiceConfig:
    return PolyVoiceConfig(
        mode=os.getenv("POLYVOICE_MODE", "mock"),
        default_source_language=os.getenv("POLYVOICE_DEFAULT_SOURCE_LANGUAGE", "en"),
        default_target_language=os.getenv("POLYVOICE_DEFAULT_TARGET_LANGUAGE", "ig"),
        max_response_words=int(os.getenv("POLYVOICE_MAX_RESPONSE_WORDS", "15")),
        latency_target_ms=float(os.getenv("POLYVOICE_LATENCY_TARGET_MS", "200")),
        stt_provider=os.getenv("POLYVOICE_STT_PROVIDER", "deepgram"),
        deepgram_api_key=os.getenv("DEEPGRAM_API_KEY"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        google_translate_api_key=os.getenv("GOOGLE_TRANSLATE_API_KEY"),
        google_application_credentials=os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        groq_api_key=os.getenv("GROQ_API_KEY"),
        elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY"),
        elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID"),
        whatsapp_access_token=os.getenv("WHATSAPP_ACCESS_TOKEN"),
        whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID"),
        whatsapp_verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN"),
        whatsapp_api_version=os.getenv("WHATSAPP_API_VERSION", "v20.0"),
    )
