from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    is_final: bool = True
    confidence: float = 1.0


@dataclass(frozen=True)
class Translation:
    text: str
    source_language: str
    target_language: str
    confidence: float = 1.0


@dataclass(frozen=True)
class AudioChunk:
    data: bytes
    sample_rate: int
    mime_type: str = "audio/pcm"
    text: str | None = None
    language: str | None = None


class SpeechToTextProvider(Protocol):
    async def transcribe_text(self, text: str, source_language: str) -> Transcript:
        """Development shortcut for text-mode input."""

    async def transcribe_audio(
        self,
        audio: bytes,
        mime_type: str,
        source_language: str | None = None,
    ) -> Transcript:
        """Transcribe an audio file or voice-note payload."""


class TranslationProvider(Protocol):
    async def detect_language(self, text: str) -> str:
        """Detect the language code for a piece of text."""

    async def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> Translation:
        """Translate text between languages."""


class ConversationProvider(Protocol):
    async def respond(self, text: str, language: str, max_words: int) -> str:
        """Generate a concise conversational response."""


class TextToSpeechProvider(Protocol):
    async def synthesize(self, text: str, language: str) -> AudioChunk:
        """Convert translated response text into speech audio."""


class AudioOutputProvider(Protocol):
    async def play(self, audio: AudioChunk) -> None:
        """Route generated audio to the selected output device."""
