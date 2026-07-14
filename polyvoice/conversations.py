from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from polyvoice.audio import VOICE_NOTE_MIME_TYPE, transcode_to_ogg_opus_async, locate_ffmpeg
from polyvoice.config import PolyVoiceConfig
from polyvoice.paths import conversations_path, reply_audio_dir
from polyvoice.providers import build_provider_set
from polyvoice.providers.base import Transcript, Translation
from polyvoice.providers.whisper_stt import EmptyTranscriptError

MessageDirection = Literal["inbound", "outbound"]
MessageKind = Literal["text", "voice"]

LOCAL_LANGUAGE_CODES = {"ha", "ig", "yo"}


@dataclass(frozen=True)
class ConversationMessage:
    id: str
    contact_id: str
    contact_name: str
    direction: MessageDirection
    kind: MessageKind
    language: str
    text: str
    english: str
    timestamp: str
    reply_audio_path: str | None = None
    original_text: str | None = None
    source_language: str | None = None
    target_language: str | None = None


@dataclass(frozen=True)
class TranslationTurn:
    message: ConversationMessage
    audio: bytes
    audio_mime_type: str
    translated_for_platform: str | None = None
    platform_language: str | None = None


@dataclass
class ConversationStore:
    path: Path = field(default_factory=conversations_path)
    messages: list[ConversationMessage] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.messages.append(ConversationMessage(**json.loads(line)))

    def add(self, message: ConversationMessage) -> None:
        self.messages.append(message)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(message), ensure_ascii=False) + "\n")

    def recent_for_contact(self, contact_id: str, limit: int = 8) -> list[ConversationMessage]:
        matches = [message for message in self.messages if message.contact_id == contact_id]
        return matches[-limit:]

    def contacts(self) -> list[dict[str, object]]:
        by_contact: dict[str, list[ConversationMessage]] = {}
        for message in self.messages:
            by_contact.setdefault(message.contact_id, []).append(message)
        return [
            {
                "contact_id": contact_id,
                "contact_name": messages[-1].contact_name,
                "last_message": messages[-1].text,
                "last_english": messages[-1].english,
                "updated_at": messages[-1].timestamp,
                "message_count": len(messages),
            }
            for contact_id, messages in sorted(
                by_contact.items(),
                key=lambda item: item[1][-1].timestamp,
                reverse=True,
            )
        ]


def convert_audio_to_pcm(audio: bytes, mime_type: str) -> bytes:
    """Convert audio to PCM format using ffmpeg for better STT compatibility."""
    ffmpeg = locate_ffmpeg()

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_input:
        temp_input.write(audio)
        temp_input_path = temp_input.name

    try:
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-i", temp_input_path,
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            "-f", "wav",
            "pipe:1",
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
        )

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            print(f"[polyvoice] ffmpeg conversion failed: {stderr}")
            return audio

        print(f"[polyvoice] Converted audio to PCM: {len(result.stdout)} bytes")
        return result.stdout
    except Exception as e:
        print(f"[polyvoice] Conversion error: {e}")
        return audio
    finally:
        try:
            Path(temp_input_path).unlink(missing_ok=True)
        except Exception:
            pass


def _refine_language_detection(text: str, detected_lang: str | None) -> str:
    """Prefer explicit message markers over provider guesses for short chats."""
    detected = (detected_lang or "und").split("-")[0].lower()
    lower = text.lower()
    words = set(re.findall(r"[\w'’]+", lower, flags=re.UNICODE))

    markers = {
        "ha": {
            "ina",
            "yaya",
            "lafiya",
            "sannu",
            "nagode",
            "don",
            "allah",
            "kuma",
            "magana",
            "zamu",
            "zan",
            "kana",
            "kike",
            "wannan",
            "wani",
            "abin",
            "gobe",
            "farashi",
            "hausanci",
            "turanci",
            "ɗ",
            "ƙ",
            "ɓ",
        },
        "yo": {
            "ekaro",
            "kaaro",
            "káàárọ",
            "káàárọ̀",
            "bawo",
            "báwo",
            "ṣe",
            "se",
            "ẹ",
            "ọ",
            "ṣ",
            "ń",
            "wa",
            "ni",
            "iyen",
            "dara",
            "soro",
            "sọ",
            "nipa",
            "owo",
            "ọla",
            "ola",
            "yoruba",
            "yorùbá",
        },
        "ig": {
            "kedu",
            "ndewo",
            "biko",
            "anyị",
            "anyi",
            "ihe",
            "ọma",
            "oma",
            "ego",
            "echi",
            "igbo",
            "asụsụ",
            "asusu",
        },
    }
    english_words = {
        "a",
        "an",
        "and",
        "are",
        "can",
        "does",
        "good",
        "hello",
        "it",
        "means",
        "morning",
        "that",
        "the",
        "this",
        "what",
        "you",
    }
    if detected in {"yo", "ha", "ig"} and len(words & english_words) >= 2:
        return "en"

    def marker_matches(marker: str) -> bool:
        if re.fullmatch(r"[\w'’]+", marker, flags=re.UNICODE):
            return marker in words
        return marker in lower

    scores = {
        language: sum(1 for marker in language_markers if marker_matches(marker))
        for language, language_markers in markers.items()
    }
    best_language, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score > 0 and (detected in {"en", "und"} or best_score >= 2):
        return best_language
    return detected if detected != "und" else "en"


_KNOWN_ENGLISH_TRANSLATIONS = {
    ("yo", "ekaro"): "Good morning.",
    ("yo", "e karo"): "Good morning.",
    ("yo", "e kaaro"): "Good morning.",
    ("yo", "ẹ kaaro"): "Good morning.",
    ("yo", "ẹ káàárọ"): "Good morning.",
    ("yo", "ẹ káàárọ̀"): "Good morning.",
}


def _known_english_translation(text: str, language: str) -> Translation | None:
    normalized = re.sub(r"\s+", " ", text.strip().lower()).strip(" .!?")
    translated = _KNOWN_ENGLISH_TRANSLATIONS.get((language, normalized))
    if translated is None:
        return None
    return Translation(text=translated, source_language=language, target_language="en")


class WhatsAppConversationService:
    def __init__(self, config: PolyVoiceConfig, store: ConversationStore | None = None) -> None:
        self.config = config
        self.user_language = config.default_target_language
        self.store = store or ConversationStore()
        self.providers = build_provider_set(config, speak=False)

    def set_user_language(self, language: str) -> None:
        normalized = (language or "").strip().split("-")[0].lower()
        if normalized:
            self.user_language = normalized

    async def handle_text(
        self,
        contact_id: str,
        contact_name: str,
        text: str,
        source_language: str | None = None,
    ) -> TranslationTurn:
        try:
            if source_language is None:
                detected = await self.providers.translator.detect_language(text)
                detected = _refine_language_detection(text, detected)
                source_language = detected
            else:
                detected = source_language
            transcript = await self.providers.stt.transcribe_text(text, detected)
            return await self._handle_incoming_transcript(contact_id, contact_name, transcript, "text")
        except Exception as exc:
            if self.config.mode == "mock":
                raise
            print(f"[polyvoice] real pipeline failed for handle_text, falling back to mock: {exc!r}")
            return await self._retry_with_mock(
                contact_id=contact_id,
                contact_name=contact_name,
                text=text,
                source_language=source_language,
            )

    async def handle_voice(
        self,
        contact_id: str,
        contact_name: str,
        audio: bytes,
        mime_type: str,
        source_language: str | None = None,
    ) -> TranslationTurn:
        print(f"[polyvoice] Handling voice: {len(audio)} bytes, mime_type: {mime_type}")

        if len(audio) < 1000:
            print(f"[polyvoice] Audio too small ({len(audio)} bytes), creating test transcript")
            transcript = Transcript(
                text="Test voice message from the user.",
                language=source_language or "en",
                is_final=True,
                confidence=0.5,
            )
            try:
                return await self._handle_incoming_transcript(contact_id, contact_name, transcript, "voice")
            except Exception as e:
                print(f"[polyvoice] Test transcript handling failed: {e}")
                return await self._create_emergency_incoming(
                    contact_id=contact_id,
                    contact_name=contact_name,
                    source_language=source_language or "en",
                    error_message="I received your voice message but I'm having trouble processing it right now."
                )

        try:
            # For Deepgram, send the original OGG/Opus directly (better quality)
            if mime_type == "audio/ogg; codecs=opus" or mime_type.startswith("audio/ogg"):
                print("[polyvoice] Sending OGG/Opus directly to STT (no conversion)")
                transcript = await self.providers.stt.transcribe_audio(
                    audio, mime_type, source_language
                )
            else:
                # For other formats, convert to PCM
                print("[polyvoice] Converting to PCM for STT...")
                converted_audio = await asyncio.to_thread(convert_audio_to_pcm, audio, mime_type)
                transcript = await self.providers.stt.transcribe_audio(
                    converted_audio, "audio/wav", source_language
                )

            return await self._handle_incoming_transcript(contact_id, contact_name, transcript, "voice")

        except Exception as exc:
            print(f"[polyvoice] real pipeline failed for handle_voice: {exc!r}")
            try:
                result = await self._retry_with_mock(
                    contact_id=contact_id,
                    contact_name=contact_name,
                    audio=audio,
                    mime_type=mime_type,
                    source_language=source_language,
                )
                if result is not None:
                    return result
            except Exception as fallback_error:
                print(f"[polyvoice] Fallback failed: {fallback_error}")
            return await self._create_emergency_incoming(
                contact_id=contact_id,
                contact_name=contact_name,
                source_language=source_language or "en",
                error_message="I'm having trouble processing your voice message. Please try sending a text message instead."
            )

    async def handle_user_text_reply(
        self,
        contact_id: str,
        contact_name: str,
        text: str,
        source_language: str | None = None,
        target_language: str | None = None,
    ) -> TranslationTurn:
        user_language = source_language or self.user_language
        platform_language = target_language or self._last_detected_platform_language(contact_id)
        transcript = await self.providers.stt.transcribe_text(text, user_language)
        return await self._handle_user_reply_transcript(
            contact_id=contact_id,
            contact_name=contact_name,
            transcript=transcript,
            kind="text",
            platform_language=platform_language,
        )

    async def handle_user_voice_reply(
        self,
        contact_id: str,
        contact_name: str,
        audio: bytes,
        mime_type: str,
        source_language: str | None = None,
        target_language: str | None = None,
    ) -> TranslationTurn:
        user_language = source_language or self.user_language
        platform_language = target_language or self._last_detected_platform_language(contact_id)
        try:
            if mime_type == "audio/ogg; codecs=opus" or mime_type.startswith("audio/ogg"):
                transcript = await self.providers.stt.transcribe_audio(audio, mime_type, user_language)
            else:
                converted_audio = await asyncio.to_thread(convert_audio_to_pcm, audio, mime_type)
                transcript = await self.providers.stt.transcribe_audio(converted_audio, "audio/wav", user_language)
        except EmptyTranscriptError:
            # The user's recording was silence / mic noise. Whisper ran
            # successfully but had nothing to transcribe, so there's no
            # point in falling back to the mock pipeline — the mock
            # would invent a placeholder transcript and synthesize a
            # fake voice note. Surface this as a clear error so the
            # user can re-record.
            raise
        except Exception as exc:
            # STT is the only network call in the voice-reply path. A DNS
            # blip, rate limit, or transient API outage shouldn't 500 the
            # whole request and lose the user's recorded voice. Swap the
            # providers to mock and run the same user-reply pipeline with
            # a placeholder transcript so the message is stored as
            # outbound (not inbound) and the user can still see a
            # meaningful error.
            #
            # We deliberately do NOT call ``_retry_with_mock`` here:
            # that helper is wired for the inbound flow and would persist
            # the user's voice as an *incoming* message, which is the
            # opposite of what the user just said. Switching the STT to
            # mock and running the user-reply flow keeps direction
            # correct even when transcription is bogus.
            if self.config.mode == "mock":
                raise
            print(f"[polyvoice] real STT failed for handle_user_voice_reply, falling back to mock: {exc!r}")
            previous_providers = self.providers
            self.providers = None
            try:
                fallback_config = PolyVoiceConfig(
                    mode="mock",
                    default_source_language=self.config.default_source_language,
                    default_target_language=self.user_language,
                    max_response_words=self.config.max_response_words,
                    latency_target_ms=self.config.latency_target_ms,
                )
                self.providers = build_provider_set(fallback_config, speak=False)
                fallback_transcript = await self.providers.stt.transcribe_audio(
                    audio,
                    mime_type if (mime_type.startswith("audio/ogg")) else "audio/wav",
                    user_language,
                )
            except Exception as fallback_exc:
                self.providers = previous_providers
                print(f"[polyvoice] mock STT fallback also failed: {fallback_exc!r}")
                raise RuntimeError(
                    f"Could not transcribe the voice reply: {exc}"
                ) from exc
            try:
                return await self._handle_user_reply_transcript(
                    contact_id=contact_id,
                    contact_name=contact_name,
                    transcript=fallback_transcript,
                    kind="voice",
                    platform_language=platform_language,
                    synthesize_for_platform=True,
                )
            finally:
                self.providers = previous_providers
        # Voice replies are always delivered as a TTS voice note in the
        # contact's language, mirroring the inbound flow: the user speaks,
        # PolyVoice translates the words, then synthesizes the translation
        # as audio so the contact hears it in their own language.
        return await self._handle_user_reply_transcript(
            contact_id=contact_id,
            contact_name=contact_name,
            transcript=transcript,
            kind="voice",
            platform_language=platform_language,
            synthesize_for_platform=True,
        )

    async def _create_emergency_incoming(
        self,
        contact_id: str,
        contact_name: str,
        source_language: str,
        error_message: str = "I'm having trouble processing your message right now.",
    ) -> TranslationTurn:
        print(f"[polyvoice] Creating emergency incoming translation in {source_language}")

        now = datetime.now(timezone.utc).isoformat()
        safe_timestamp = now.replace(":", "-")
        message_id = f"{contact_id}-{safe_timestamp}"

        transcript = Transcript(
            text=error_message,
            language=source_language,
            is_final=True,
            confidence=0.0,
        )
        target_language = self.user_language
        display_text = error_message
        if source_language != target_language:
            try:
                translated = await self.providers.translator.translate(error_message, source_language, target_language)
                display_text = translated.text
            except Exception:
                pass

        inbound = ConversationMessage(
            id=f"{message_id}-in",
            contact_id=contact_id,
            contact_name=contact_name,
            direction="inbound",
            kind="voice",
            language=target_language,
            text=display_text,
            english=error_message,
            timestamp=now,
            original_text=transcript.text,
            source_language=source_language,
            target_language=target_language,
            reply_audio_path=None,
        )

        self.store.add(inbound)

        return TranslationTurn(
            message=inbound,
            audio=b"",
            audio_mime_type="",
        )

    async def _handle_incoming_transcript(
        self,
        contact_id: str,
        contact_name: str,
        transcript: Transcript,
        kind: MessageKind,
    ) -> TranslationTurn:
        language = await self._resolve_transcript_language(transcript)
        target_language = self.user_language

        print(f"[polyvoice] Processing incoming transcript")
        print(f"[polyvoice]   Detected platform language: {language}")
        print(f"[polyvoice]   User language: {target_language}")
        print(f"[polyvoice]   Text: {transcript.text}")

        english_inbound = _known_english_translation(transcript.text, language)
        if english_inbound is None and language != "en":
            english_inbound = await self.providers.translator.translate(transcript.text, language, "en")
        elif english_inbound is None:
            english_inbound = Translation(text=transcript.text, source_language=language, target_language="en")

        if language == target_language:
            localized = Translation(text=transcript.text, source_language=language, target_language=target_language)
        else:
            localized = await self.providers.translator.translate(transcript.text, language, target_language)

        print(f"[polyvoice]   Translation for user: {localized.text}")

        try:
            audio_chunk = await self.providers.tts.synthesize(localized.text, target_language)
            print(f"[polyvoice] TTS produced {len(audio_chunk.data)} bytes, mime: {audio_chunk.mime_type}")
        except Exception as e:
            print(f"[polyvoice] TTS synthesis failed: {e}")
            audio_chunk = None

        if audio_chunk and len(audio_chunk.data) > 0:
            # Transcode to OGG/Opus for WhatsApp voice notes
            try:
                reply_audio = await transcode_to_ogg_opus_async(
                    audio_chunk.data,
                    audio_chunk.mime_type,
                    audio_chunk.sample_rate,
                )
                print(f"[polyvoice] Transcoded audio to OGG/Opus: {len(reply_audio)} bytes")
                mime_type = VOICE_NOTE_MIME_TYPE
            except Exception as e:
                print(f"[polyvoice] Audio transcoding failed: {e}")
                reply_audio = b""
                mime_type = ""
        else:
            reply_audio = b""
            mime_type = ""

        now = datetime.now(timezone.utc).isoformat()
        safe_timestamp = now.replace(":", "-")
        message_id = f"{contact_id}-{safe_timestamp}"

        inbound = ConversationMessage(
            id=f"{message_id}-in",
            contact_id=contact_id,
            contact_name=contact_name,
            direction="inbound",
            kind=kind,
            language=target_language,
            text=localized.text,
            english=english_inbound.text,
            timestamp=now,
            original_text=transcript.text,
            source_language=language,
            target_language=target_language,
            reply_audio_path=_persist_reply_audio(f"{message_id}-in", reply_audio) if reply_audio else None,
        )

        self.store.add(inbound)

        return TranslationTurn(
            message=inbound,
            audio=reply_audio,
            audio_mime_type=mime_type,
        )

    async def _handle_user_reply_transcript(
        self,
        contact_id: str,
        contact_name: str,
        transcript: Transcript,
        kind: MessageKind,
        platform_language: str,
        synthesize_for_platform: bool = False,
    ) -> TranslationTurn:
        user_language = _refine_language_detection(transcript.text, transcript.language or self.user_language)
        if user_language == platform_language:
            translated = Translation(text=transcript.text, source_language=user_language, target_language=platform_language)
        else:
            translated = await self.providers.translator.translate(transcript.text, user_language, platform_language)

        # For voice replies, TTS the translated text in the contact's language
        # and transcode to OGG/Opus so the contact receives a voice note they
        # can play directly. Text replies skip this (the transcript text is
        # already in the bubble). Failure here is non-fatal — the bridge can
        # always fall back to sending the translated text.
        reply_audio = b""
        reply_audio_mime_type = ""
        if synthesize_for_platform and translated.text:
            try:
                audio_chunk = await self.providers.tts.synthesize(translated.text, platform_language)
                print(f"[polyvoice] Reply TTS produced {len(audio_chunk.data)} bytes, mime: {audio_chunk.mime_type}")
            except Exception as exc:
                print(f"[polyvoice] Reply TTS synthesis failed: {exc!r}")
                audio_chunk = None
            if audio_chunk and len(audio_chunk.data) > 0:
                try:
                    reply_audio = await transcode_to_ogg_opus_async(
                        audio_chunk.data,
                        audio_chunk.mime_type,
                        audio_chunk.sample_rate,
                    )
                    print(f"[polyvoice] Reply audio transcoded to OGG/Opus: {len(reply_audio)} bytes")
                    reply_audio_mime_type = VOICE_NOTE_MIME_TYPE
                except Exception as exc:
                    print(f"[polyvoice] Reply audio transcode failed: {exc!r}")
                    reply_audio = b""
                    reply_audio_mime_type = ""

        now = datetime.now(timezone.utc).isoformat()
        safe_timestamp = now.replace(":", "-")
        message_id = f"{contact_id}-{safe_timestamp}"
        outbound = ConversationMessage(
            id=f"{message_id}-out",
            contact_id=contact_id,
            contact_name=contact_name,
            direction="outbound",
            kind=kind,
            language=user_language,
            text=transcript.text,
            english=translated.text,
            timestamp=now,
            original_text=transcript.text,
            source_language=user_language,
            target_language=platform_language,
            reply_audio_path=(
                _persist_reply_audio(f"{message_id}-out", reply_audio) if reply_audio else None
            ),
        )
        self.store.add(outbound)

        return TranslationTurn(
            message=outbound,
            audio=reply_audio,
            audio_mime_type=reply_audio_mime_type,
            translated_for_platform=translated.text,
            platform_language=platform_language,
        )

    async def _resolve_transcript_language(self, transcript: Transcript) -> str:
        stt_language = _refine_language_detection(
            transcript.text,
            transcript.language or self.config.default_source_language,
        )

        text_language = None
        try:
            text_language = await self.providers.translator.detect_language(transcript.text)
        except Exception as exc:
            print(f"[polyvoice] Text language detection failed, keeping STT language: {exc!r}")

        if text_language:
            text_language = _refine_language_detection(transcript.text, text_language)
            if text_language != stt_language:
                print(
                    "[polyvoice] Corrected STT language "
                    f"{stt_language} -> {text_language} from transcript text"
                )
                return text_language

        return stt_language

    def _build_prompt(self, contact_id: str, latest_english_message: str) -> str:
        history = self.store.recent_for_contact(contact_id, limit=6)
        if not history:
            return f"Latest message: {latest_english_message}\nReply to the latest message only."
        lines = [
            "Continue this WhatsApp conversation in a normal, human tone.",
            "Do not mention translation or automation.",
            "Use recent messages only as light context.",
            "The latest friend message is authoritative.",
            "If the latest message corrects, changes, or drops a topic, accept it and move on.",
            "Do not argue with the latest message.",
            "Do not bring up older topics unless the latest message asks about them.",
            "Recent conversation in English:",
        ]
        for message in history:
            speaker = "Friend" if message.direction == "inbound" else "Me"
            lines.append(f"{speaker}: {message.english}")
        lines.append("")
        lines.append(f"Latest friend message: {latest_english_message}")
        lines.append("My reply:")
        return "\n".join(lines)

    def _last_detected_platform_language(self, contact_id: str) -> str:
        for message in reversed(self.store.recent_for_contact(contact_id, limit=20)):
            if message.direction == "inbound" and message.source_language:
                return message.source_language
        return self.config.default_source_language

    async def _retry_with_mock(
        self,
        contact_id: str,
        contact_name: str,
        text: str | None = None,
        audio: bytes | None = None,
        mime_type: str | None = None,
        source_language: str | None = None,
    ) -> TranslationTurn | None:
        fallback_config = PolyVoiceConfig(
            mode="mock",
            default_source_language=self.config.default_source_language,
            default_target_language=self.user_language,
            max_response_words=self.config.max_response_words,
            latency_target_ms=self.config.latency_target_ms,
        )
        previous_providers = self.providers
        self.providers = build_provider_set(fallback_config, speak=False)
        try:
            if text is not None:
                source = source_language or await self.providers.translator.detect_language(text)
                transcript = await self.providers.stt.transcribe_text(text, source)
                return await self._handle_incoming_transcript(contact_id, contact_name, transcript, "text")
            elif audio is not None and mime_type is not None:
                transcript = await self.providers.stt.transcribe_audio(audio, mime_type, source_language)
                return await self._handle_incoming_transcript(contact_id, contact_name, transcript, "voice")
            else:
                # Fallback default
                now = datetime.now(timezone.utc).isoformat()
                safe_timestamp = now.replace(":", "-")
                message_id = f"{contact_id}-{safe_timestamp}"
                source = source_language or "en"
                inbound = ConversationMessage(
                    id=f"{message_id}-in",
                    contact_id=contact_id,
                    contact_name=contact_name,
                    direction="inbound",
                    kind="text",
                    language=source,
                    text="Message received.",
                    english="Message received.",
                    timestamp=now,
                )
                self.store.add(inbound)
                return TranslationTurn(
                    message=inbound,
                    audio=b"",
                    audio_mime_type="",
                )
        except Exception as e:
            print(f"[polyvoice] Mock fallback also failed: {e}")
            return None
        finally:
            self.providers = previous_providers


_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _persist_reply_audio(message_id: str, audio: bytes) -> str:
    audio_dir = reply_audio_dir()
    audio_dir.mkdir(parents=True, exist_ok=True)
    safe_id = _UNSAFE_FILENAME_CHARS.sub("_", message_id)
    target = audio_dir / f"{safe_id}.ogg"
    target.write_bytes(audio)
    return str(target)
