from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from polyvoice.config import PolyVoiceConfig
from polyvoice.providers.base import (
    AudioOutputProvider,
    ConversationProvider,
    SpeechToTextProvider,
    TextToSpeechProvider,
    TranslationProvider,
)
from polyvoice.providers.deepgram import DeepgramSpeechToTextProvider
from polyvoice.providers.whisper_stt import WhisperSpeechToTextProvider  # Add this
from polyvoice.providers.elevenlabs_tts import ElevenLabsTextToSpeechProvider
from polyvoice.providers.google_translate import GoogleTranslationProvider
from polyvoice.providers.groq_conversation import GroqConversationProvider
from polyvoice.providers.mock import (
    MockAudioOutputProvider,
    MockProviderSet,
    build_mock_provider_set,
)


@dataclass(frozen=True)
class RealProviderSet:
    stt: SpeechToTextProvider
    translator: TranslationProvider
    conversation: ConversationProvider
    tts: TextToSpeechProvider
    audio_output: AudioOutputProvider


ProviderSet = Union[RealProviderSet, MockProviderSet]


def build_real_provider_set(
        config: PolyVoiceConfig,
        audio_output_path: str | None = None,
        speak: bool = False,
) -> RealProviderSet:
    """Construct a fully-wired set of real API-backed providers."""
    missing: list[str] = []

    # Check STT provider credentials
    if config.stt_provider == "deepgram":
        if not config.deepgram_api_key:
            missing.append("DEEPGRAM_API_KEY")
    elif config.stt_provider == "whisper":
        if not config.openai_api_key:
            missing.append("OPENAI_API_KEY")
    else:
        missing.append(f"Unknown STT provider: {config.stt_provider}")

    # Check other required credentials
    if not config.groq_api_key:
        missing.append("GROQ_API_KEY")
    if not config.elevenlabs_api_key:
        missing.append("ELEVENLABS_API_KEY")
    if not config.elevenlabs_voice_id:
        missing.append("ELEVENLABS_VOICE_ID")
    if not (config.google_translate_api_key or config.google_application_credentials):
        missing.append("GOOGLE_TRANSLATE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS")

    if missing:
        raise RuntimeError(
            "POLYVOICE_MODE=real requires the following env vars to be set in .env: "
            + ", ".join(missing)
        )

    # Select STT provider
    if config.stt_provider == "deepgram":
        stt = DeepgramSpeechToTextProvider(api_key=config.deepgram_api_key)  # type: ignore[arg-type]
    else:  # whisper
        stt = WhisperSpeechToTextProvider(api_key=config.openai_api_key)  # type: ignore[arg-type]

    return RealProviderSet(
        stt=stt,
        translator=GoogleTranslationProvider(
            api_key=config.google_translate_api_key,
            credentials_path=config.google_application_credentials,
        ),
        conversation=GroqConversationProvider(api_key=config.groq_api_key),  # type: ignore[arg-type]
        tts=ElevenLabsTextToSpeechProvider(
            api_key=config.elevenlabs_api_key,  # type: ignore[arg-type]
            voice_id=config.elevenlabs_voice_id,  # type: ignore[arg-type]
        ),
        audio_output=MockAudioOutputProvider(output_path=audio_output_path, speak=speak),
    )


def build_provider_set(
        config: PolyVoiceConfig,
        audio_output_path: str | None = None,
        speak: bool = False,
) -> ProviderSet:
    """Dispatch to real or mock providers based on `config.mode`."""
    if config.mode == "mock":
        return build_mock_provider_set(output_path=audio_output_path, speak=speak)
    return build_real_provider_set(config, audio_output_path=audio_output_path, speak=speak)