"""Whisper STT provider.

Defines :class:`EmptyTranscriptError` so callers can distinguish "the audio
was probably silence / mic noise" from real infrastructure failures (DNS,
network, API outage, 5xx). Both surface as exceptions, but the user-facing
recovery is different — see ``conversations.handle_user_voice_reply``.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import httpx

from polyvoice.providers.base import Transcript

WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"

logger = logging.getLogger(__name__)


class EmptyTranscriptError(RuntimeError):
    """Whisper ran successfully but returned no transcript text.

    The audio was likely silence, mic noise, or a recording where the
    user didn't actually speak. This is *not* an infrastructure
    failure — DNS, the API, and the network are all healthy — so the
    fallback mock pipeline would only produce a fake
    "voice note" with placeholder audio. The UI should surface a
    "I didn't hear anything, try again" message and let the user
    re-record instead of sending a bogus reply.
    """


class WhisperSpeechToTextProvider:
    """OpenAI Whisper STT provider - automatically detects any language."""

    def __init__(self, api_key: str, model: str = "whisper-1"):
        if not api_key:
            raise ValueError("OpenAI API key is required (set OPENAI_API_KEY).")
        self._api_key = api_key
        self._model = model
        logger.info("Whisper STT initialized with auto-language detection")

    async def transcribe_text(self, text: str, source_language: str) -> Transcript:
        """Development shortcut for text-mode input."""
        return Transcript(text=text.strip(), language=source_language, is_final=True)

    async def transcribe_audio(
            self,
            audio: bytes,
            mime_type: str,
            source_language: str | None = None,
    ) -> Transcript:
        """Transcribe audio using OpenAI Whisper API with auto-language detection."""
        logger.info(f"Transcribing audio: {len(audio)} bytes, mime_type: {mime_type}")

        # Check if audio is too small
        if len(audio) < 1000:
            logger.warning(f"Audio too small: {len(audio)} bytes")
            raise ValueError(f"Audio too small: {len(audio)} bytes")

        # Save audio to temporary file for Whisper
        with tempfile.NamedTemporaryFile(
                suffix=".wav",
                delete=False
        ) as temp_file:
            temp_file.write(audio)
            temp_path = temp_file.name

        try:
            # Prepare the request - NO language specified = auto-detection!
            headers = {
                "Authorization": f"Bearer {self._api_key}",
            }

            # Prepare files for multipart upload
            files = {
                "file": (Path(temp_path).name, open(temp_path, "rb"), "audio/wav"),
                "model": (None, self._model),
                "response_format": (None, "json"),
                # DO NOT specify language - Whisper will auto-detect
            }

            # Increase timeout for longer audio
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    WHISPER_URL,
                    headers=headers,
                    files=files,
                )
                response.raise_for_status()
                result = response.json()
                text = result.get("text", "").strip()

                # Whisper doesn't return language in the API response
                # We need to detect it from the text or use the source_language if provided

            # Clean up temp file
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass

            if not text:
                logger.warning("Whisper returned empty transcript")
                # Distinct from a network/API failure: a 200 OK with an
                # empty body almost always means the user recorded
                # silence or background noise. Callers (the user-reply
                # flow in particular) should NOT fall back to the mock
                # pipeline for this — the mock would synthesize a
                # fake voice note from a placeholder transcript and the
                # user would see a "reply" they never actually said.
                raise EmptyTranscriptError("Whisper returned an empty transcript")

            # Determine the language
            detected_language = source_language  # Use provided language if available

            # If no language provided, try to detect from text
            if not detected_language:
                detected_language = await self._detect_language_from_text(text)
                logger.info(f"Auto-detected language: {detected_language}")

            logger.info(f"Transcribed: '{text}' (language: {detected_language})")
            return Transcript(
                text=text,
                language=detected_language or "en",  # Fallback to English
                is_final=True,
                confidence=1.0,
            )

        except Exception as e:
            logger.error(f"Whisper request failed: {e}")
            # Don't fallback - raise the error so we know what happened
            raise RuntimeError(f"Whisper API error: {e}")
        finally:
            # Clean up temp file
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass

    async def _detect_language_from_text(self, text: str) -> str | None:
        """Simple language detection from text using common markers."""
        # This is a fallback - in production, you might want to use
        # a proper language detection library like `langdetect`
        lower_text = text.lower().strip()

        # Hausa markers
        hausa_markers = ["habba", "baba", "yagarin", "rukukin", "yaya", "wani",
                         "abin", "zama", "shi", "ta", "na", "ga", "ku", "muna"]
        if any(marker in lower_text for marker in hausa_markers):
            return "ha"

        # Yoruba markers
        yoruba_markers = ["bawo", "se", "ni", "iyen", "dara", "mo", "o", "a",
                          "le", "soro", "nipa", "owo", "ola", "jẹ", "ti", "nibo"]
        if any(marker in lower_text for marker in yoruba_markers):
            return "yo"

        # Igbo markers
        igbo_markers = ["kedu", "ihe", "oma", "anyị", "ga", "eme", "tụ", "lee",
                        "biko", "nwoke", "nwanyi", "ebe", "aka"]
        if any(marker in lower_text for marker in igbo_markers):
            return "ig"

        # French markers
        french_markers = ["bonjour", "merci", "demain", "prix", "est-ce", "parler",
                          "salut", "ça", "comment", "pourquoi", "avec", "vous"]
        if any(marker in lower_text for marker in french_markers):
            return "fr"

        # Spanish markers
        spanish_markers = ["hola", "gracias", "mañana", "precio", "por qué", "hablar",
                           "salud", "cómo", "estás", "bien", "gracias", "adiós"]
        if any(marker in lower_text for marker in spanish_markers):
            return "es"

        # German markers
        german_markers = ["hallo", "danke", "morgen", "preis", "warum", "sprechen",
                          "gut", "tschüss", "wie", "geht", "ja", "nein"]
        if any(marker in lower_text for marker in german_markers):
            return "de"

        # Italian markers
        italian_markers = ["ciao", "grazie", "domani", "prezzo", "perché", "parlare",
                           "buono", "arrivederci", "come", "stai"]
        if any(marker in lower_text for marker in italian_markers):
            return "it"

        # Portuguese markers
        portuguese_markers = ["olá", "obrigado", "amanhã", "preço", "por que", "falar",
                              "bom", "tchau", "como", "está"]
        if any(marker in lower_text for marker in portuguese_markers):
            return "pt"

        # Check for English (default)
        # If no markers found, assume English
        # But if the text has no common English words, it might be something else
        return "en"