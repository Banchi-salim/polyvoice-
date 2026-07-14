from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from polyvoice.config import PolyVoiceConfig
from polyvoice.orchestrator import StreamingOrchestrator
from polyvoice.providers import build_mock_provider_set
from polyvoice.voice import create_voice_profile


class StreamingOrchestratorTests(unittest.TestCase):
    def test_process_text_returns_translated_audio_result(self) -> None:
        result = asyncio.run(self._run_pipeline())

        self.assertEqual(result.transcript, "Can we discuss pricing tomorrow?")
        self.assertEqual(result.inbound_translation, "Can we discuss pricing tomorrow?")
        self.assertEqual(result.input_translation, "Se a le soro nipa owo ni ola?")
        self.assertEqual(
            result.outbound_translation,
            "Beeni, iyen dara. Mo le jerisi awon alaye naa bayi.",
        )
        self.assertGreater(result.audio_bytes, 0)
        self.assertTrue(result.within_target)

    async def _run_pipeline(self):
        config = PolyVoiceConfig()
        providers = build_mock_provider_set()
        orchestrator = StreamingOrchestrator(
            config=config,
            stt=providers.stt,
            translator=providers.translator,
            conversation=providers.conversation,
            tts=providers.tts,
            audio_output=providers.audio_output,
        )
        return await orchestrator.process_text(
            "Can we discuss pricing tomorrow?",
            source_language="en",
            target_language="yo",
        )

    def test_process_text_speaks_translated_response_only(self) -> None:
        config = PolyVoiceConfig()
        providers = build_mock_provider_set()
        orchestrator = StreamingOrchestrator(
            config=config,
            stt=providers.stt,
            translator=providers.translator,
            conversation=providers.conversation,
            tts=providers.tts,
            audio_output=providers.audio_output,
        )

        result = asyncio.run(
            orchestrator.process_text(
                "Can we discuss pricing tomorrow?",
                source_language="en",
                target_language="yo",
            )
        )

        self.assertIsNotNone(providers.audio_output.last_chunk)
        self.assertEqual(providers.audio_output.last_chunk.text, result.outbound_translation)
        self.assertNotEqual(providers.audio_output.last_chunk.text, result.input_translation)


class VoiceProfileTests(unittest.TestCase):
    def test_create_voice_profile_from_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sample = Path(temp_dir) / "sample.wav"
            profile_dir = Path(temp_dir) / "profiles"
            sample.write_bytes(b"0" * 12_000)

            profile = create_voice_profile("Salim", [str(sample)], output_dir=profile_dir)

            self.assertEqual(profile.profile_id, "salim")
            self.assertEqual(len(profile.samples), 1)
            self.assertEqual(profile.samples[0].extension, ".wav")
            self.assertTrue((profile_dir / "salim.json").exists())


if __name__ == "__main__":
    unittest.main()
