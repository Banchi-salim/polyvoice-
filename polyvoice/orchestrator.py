from __future__ import annotations

import asyncio
from dataclasses import dataclass

from polyvoice.config import PolyVoiceConfig
from polyvoice.metrics import LatencyRecorder
from polyvoice.providers.base import (
    AudioChunk,
    AudioOutputProvider,
    ConversationProvider,
    SpeechToTextProvider,
    TranslationProvider,
    TextToSpeechProvider,
)


@dataclass(frozen=True)
class PipelineResult:
    transcript: str
    inbound_translation: str
    input_translation: str
    response: str
    outbound_translation: str
    audio_bytes: int
    audio_sample_rate: int
    stage_latency_ms: dict[str, float]
    provider_latency_ms: float
    playback_latency_ms: float
    total_latency_ms: float
    within_target: bool


class StreamingOrchestrator:
    def __init__(
        self,
        config: PolyVoiceConfig,
        stt: SpeechToTextProvider,
        translator: TranslationProvider,
        conversation: ConversationProvider,
        tts: TextToSpeechProvider,
        audio_output: AudioOutputProvider,
    ) -> None:
        self.config = config
        self.stt = stt
        self.translator = translator
        self.conversation = conversation
        self.tts = tts
        self.audio_output = audio_output

    async def process_text(
        self,
        text: str,
        source_language: str | None = None,
        target_language: str | None = None,
    ) -> PipelineResult:
        source = source_language or self.config.default_source_language
        target = target_language or self.config.default_target_language
        recorder = LatencyRecorder()

        with recorder.measure("speech_to_text"):
            transcript = await self.stt.transcribe_text(text, source)

        with recorder.measure("parallel_translate_and_respond"):
            inbound_task = asyncio.create_task(
                self.translator.translate(transcript.text, transcript.language, "en")
            )
            input_translation_task = asyncio.create_task(
                self.translator.translate(transcript.text, transcript.language, target)
            )
            try:
                response = await self.conversation.respond(
                    transcript.text,
                    "en",
                    self.config.max_response_words,
                )
            except Exception:
                inbound_task.cancel()
                input_translation_task.cancel()
                await asyncio.gather(inbound_task, input_translation_task, return_exceptions=True)
                raise

        with recorder.measure("reverse_translation"):
            outbound = await self.translator.translate(response, "en", target)

        with recorder.measure("text_to_speech"):
            audio = await self.tts.synthesize(outbound.text, target)

        with recorder.measure("audio_output"):
            await self.audio_output.play(audio)

        with recorder.measure("finish_display_translations"):
            inbound_translation, input_translation = await asyncio.gather(
                inbound_task,
                input_translation_task,
            )

        stage_latency = recorder.by_stage()
        playback_latency = stage_latency.get("audio_output", 0.0)
        display_latency = stage_latency.get("finish_display_translations", 0.0)
        total_latency = sum(stage_latency.values())
        provider_latency = total_latency - playback_latency - display_latency
        return self._build_result(
            transcript=transcript.text,
            inbound_translation=inbound_translation.text,
            input_translation=input_translation.text,
            response=response,
            outbound_translation=outbound.text,
            audio=audio,
            recorder=recorder,
            provider_latency=provider_latency,
            playback_latency=playback_latency,
            total_latency=total_latency,
        )

    def _build_result(
        self,
        transcript: str,
        inbound_translation: str,
        input_translation: str,
        response: str,
        outbound_translation: str,
        audio: AudioChunk,
        recorder: LatencyRecorder,
        provider_latency: float,
        playback_latency: float,
        total_latency: float,
    ) -> PipelineResult:
        return PipelineResult(
            transcript=transcript,
            inbound_translation=inbound_translation,
            input_translation=input_translation,
            response=response,
            outbound_translation=outbound_translation,
            audio_bytes=len(audio.data),
            audio_sample_rate=audio.sample_rate,
            stage_latency_ms=recorder.by_stage(),
            provider_latency_ms=provider_latency,
            playback_latency_ms=playback_latency,
            total_latency_ms=total_latency,
            within_target=provider_latency <= self.config.latency_target_ms,
        )
