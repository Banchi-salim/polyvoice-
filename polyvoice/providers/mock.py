from __future__ import annotations

import asyncio
import io
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from polyvoice.providers.base import AudioChunk, Transcript, Translation


LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "ha": "Hausa",
    "ig": "Igbo",
    "yo": "Yoruba",
}

PHRASEBOOK = {
    ("hi", "en", "yo"): "Bawo",
    ("hello", "en", "yo"): "Bawo",
    ("can we discuss pricing tomorrow?", "en", "yo"): "Se a le soro nipa owo ni ola?",
    ("yes, that works. i can confirm the details now.", "en", "yo"): (
        "Beeni, iyen dara. Mo le jerisi awon alaye naa bayi."
    ),
}


class MockSpeechToTextProvider:
    async def transcribe_text(self, text: str, source_language: str) -> Transcript:
        await asyncio.sleep(0.005)
        return Transcript(text=text.strip(), language=source_language, is_final=True)

    async def transcribe_audio(
        self,
        audio: bytes,
        mime_type: str,
        source_language: str | None = None,
    ) -> Transcript:
        await asyncio.sleep(0.01)
        language = source_language or "fr"
        try:
            text = audio.decode("utf-8").strip()
        except UnicodeDecodeError:
            text = "Bonjour, est-ce qu'on peut parler demain?"
        return Transcript(text=text or "Bonjour, est-ce qu'on peut parler demain?", language=language)


class MockTranslationProvider:
    async def detect_language(self, text: str) -> str:
        await asyncio.sleep(0.005)
        lowered = text.strip().lower()
        markers = {
            "fr": ("bonjour", "merci", "demain", "prix", "est-ce", "parler", "salut", "ça"),
            "es": ("hola", "gracias", "mañana", "precio", "hablar"),
            "ha": ("ina", "yaya", "lafiya", "sannu", "nagode", "hausanci", "farashi"),
            "yo": ("bawo", "báwo", "se", "ṣe", "soro", "sọ", "nipa", "owo", "yoruba"),
            "ig": ("kedu", "ndewo", "biko", "anyị", "anyi", "igbo", "ego"),
        }
        for language, language_markers in markers.items():
            if any(marker in lowered for marker in language_markers):
                return language
        return "en"

    async def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> Translation:
        await asyncio.sleep(0.01)
        if source_language == target_language:
            return Translation(
                text=text,
                source_language=source_language,
                target_language=target_language,
            )

        key = (text.strip().lower(), source_language, target_language)
        source = LANGUAGE_NAMES.get(source_language, source_language)
        target = LANGUAGE_NAMES.get(target_language, target_language)
        translated = PHRASEBOOK.get(key) or f"[{source} -> {target}] {text}"
        return Translation(
            text=translated,
            source_language=source_language,
            target_language=target_language,
        )


class MockConversationProvider:
    async def respond(self, text: str, language: str, max_words: int) -> str:
        await asyncio.sleep(0.01)
        words = "Yes, that works. I can confirm the details now.".split()
        return " ".join(words[:max_words])


class MockTextToSpeechProvider:
    async def synthesize(self, text: str, language: str) -> AudioChunk:
        await asyncio.sleep(0.015)

        # Generate a small valid PCM audio chunk for testing
        # This creates a 1-second silent audio at 16kHz
        sample_rate = 16000
        duration_samples = sample_rate * 1  # 1 second
        # Create PCM data (16-bit signed, 0 = silence)
        pcm_data = b'\x00\x00' * duration_samples

        return AudioChunk(
            data=pcm_data,
            sample_rate=sample_rate,
            mime_type="audio/pcm",  # Use PCM instead of mock type
            text=text,
            language=language,
        )


class MockAudioOutputProvider:
    def __init__(self, output_path: str | None = None, speak: bool = False) -> None:
        self.last_chunk: AudioChunk | None = None
        self._output_path = output_path
        self._speak = speak

    async def play(self, audio: AudioChunk) -> None:
        await asyncio.sleep(0.002)
        self.last_chunk = audio
        if self._output_path is not None:
            write_audio_file(Path(self._output_path), audio)
        if self._speak and audio.mime_type == "audio/pcm":
            await asyncio.to_thread(play_pcm_audio, audio.data, audio.sample_rate)
        elif self._speak and audio.mime_type == "audio/mpeg":
            await asyncio.to_thread(play_mpeg_audio, audio.data)
        elif self._speak and audio.text:
            await asyncio.to_thread(speak_text, audio.text)


def write_audio_file(path: Path, audio: AudioChunk) -> None:
    if audio.mime_type == "audio/pcm":
        path.write_bytes(pcm_to_wav_bytes(audio.data, audio.sample_rate))
        return
    path.write_bytes(audio.data)


def play_pcm_audio(audio_bytes: bytes, sample_rate: int) -> None:
    if not sys.platform.startswith("win"):
        return

    import winsound

    winsound.PlaySound(
        pcm_to_wav_bytes(audio_bytes, sample_rate),
        winsound.SND_MEMORY,
    )


def pcm_to_wav_bytes(audio_bytes: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_bytes)
    return buffer.getvalue()


def play_mpeg_audio(audio_bytes: bytes) -> None:
    if not sys.platform.startswith("win"):
        return

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_audio:
        temp_audio.write(audio_bytes)
        temp_path = temp_audio.name

    command = (
        "param([string]$Path) "
        "Add-Type -AssemblyName PresentationCore; "
        "$player = New-Object System.Windows.Media.MediaPlayer; "
        "$player.Open([Uri]::new($Path)); "
        "for ($i = 0; $i -lt 40 -and -not $player.NaturalDuration.HasTimeSpan; $i++) { "
        "Start-Sleep -Milliseconds 50 "
        "} "
        "$duration = 3000; "
        "if ($player.NaturalDuration.HasTimeSpan) { "
        "$duration = [int][Math]::Ceiling($player.NaturalDuration.TimeSpan.TotalMilliseconds) + 250 "
        "} "
        "$player.Play(); "
        "Start-Sleep -Milliseconds $duration; "
        "$player.Close()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command, temp_path],
            check=False,
            capture_output=True,
        )
    finally:
        Path(temp_path).unlink(missing_ok=True)


def speak_text(text: str) -> None:
    if not sys.platform.startswith("win"):
        return

    command = (
        "Add-Type -AssemblyName System.Speech; "
        "$text = [Console]::In.ReadToEnd(); "
        "$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$speaker.Speak($text)"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        input=text,
        text=True,
        check=False,
        capture_output=True,
    )


@dataclass(frozen=True)
class MockProviderSet:
    stt: MockSpeechToTextProvider
    translator: MockTranslationProvider
    conversation: MockConversationProvider
    tts: MockTextToSpeechProvider
    audio_output: MockAudioOutputProvider


def build_mock_provider_set(output_path: str | None = None, speak: bool = False) -> MockProviderSet:
    return MockProviderSet(
        stt=MockSpeechToTextProvider(),
        translator=MockTranslationProvider(),
        conversation=MockConversationProvider(),
        tts=MockTextToSpeechProvider(),
        audio_output=MockAudioOutputProvider(output_path=output_path, speak=speak),
    )
