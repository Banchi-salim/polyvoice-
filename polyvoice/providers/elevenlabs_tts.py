from __future__ import annotations

import httpx

from polyvoice.providers.base import AudioChunk


# ElevenLabs `eleven_turbo_v2_5` is the lowest-latency multilingual model and
# supports the cloned voices created via the Instant Voice Cloning flow.
DEFAULT_MODEL_ID = "eleven_turbo_v2_5"

# Request a complete MP3 container from ElevenLabs. The conversation service
# pipes these bytes through ffmpeg to produce OGG/Opus for WhatsApp voice
# notes (see `polyvoice/audio.py`). MP3 is the lowest-friction source format
# for ffmpeg: it has proper headers so we don't need to declare -f/-ar/-ac
# on the input side. The local CLI playback path is handled separately by
# `MockAudioOutputProvider` — it never sees these bytes.
DEFAULT_SAMPLE_RATE = 44_100
DEFAULT_MIME_TYPE = "audio/mpeg"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"


class ElevenLabsTextToSpeechProvider:
    """ElevenLabs Text-to-Speech provider."""

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model_id: str = DEFAULT_MODEL_ID,
    ) -> None:
        if not api_key:
            raise ValueError("ElevenLabs API key is required (set ELEVENLABS_API_KEY).")
        if not voice_id:
            raise ValueError("ElevenLabs voice_id is required (set ELEVENLABS_VOICE_ID).")
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id

    async def synthesize(self, text: str, language: str) -> AudioChunk:
        # `language` is accepted to match the TextToSpeechProvider Protocol
        # signature. ElevenLabs voices are language-agnostic — the model
        # auto-detects and matches the input script.
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self._voice_id}"
        params = {
            "optimize_streaming_latency": "4",
            "output_format": DEFAULT_OUTPUT_FORMAT,
        }
        headers = {
            "xi-api-key": self._api_key,
            "Accept": "application/octet-stream",
            "Content-Type": "application/json",
        }
        body = {
            "text": text,
            "model_id": self._model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, params=params, json=body, headers=headers)
            if response.status_code == 401:
                detail = _error_detail(response)
                raise RuntimeError(f"ElevenLabs authentication failed: {detail}")
            if response.status_code == 404:
                detail = _error_detail(response)
                raise RuntimeError(f"ElevenLabs voice lookup failed: {detail}")
            response.raise_for_status()
            audio_bytes = response.content

        return AudioChunk(
            data=audio_bytes,
            sample_rate=DEFAULT_SAMPLE_RATE,
            mime_type=DEFAULT_MIME_TYPE,
            text=text,
            language=language,
        )


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text

    detail = payload.get("detail")
    if isinstance(detail, dict):
        status = detail.get("status")
        message = detail.get("message")
        if status and message:
            return f"{status}: {message}"
        if message:
            return str(message)
    if isinstance(detail, str):
        return detail
    return response.text
