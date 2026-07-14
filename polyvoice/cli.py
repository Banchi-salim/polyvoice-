
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polyvoice.config import load_config
from polyvoice.config import PolyVoiceConfig
from polyvoice.orchestrator import StreamingOrchestrator
from polyvoice.providers import build_provider_set
from polyvoice.voice import create_voice_profile, list_voice_profiles


LANGUAGE_HINT = "Language code: en, yo, ha, or ig."


class FriendlyArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        raise SystemExit(f"\nPolyVoice could not start: {message}\n\nRun without arguments for interactive mode.")


def build_parser() -> argparse.ArgumentParser:
    parser = FriendlyArgumentParser(description="Run and configure the PolyVoice MVP pipeline.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Process a text message through the MVP pipeline.")
    add_run_arguments(run_parser)

    voice_parser = subparsers.add_parser("voice", help="Manage local voice samples.")
    voice_subparsers = voice_parser.add_subparsers(dest="voice_command")

    add_voice_parser = voice_subparsers.add_parser("add", help="Create a local voice profile from sample files.")
    add_voice_parser.add_argument("--name", required=True, help="Voice profile name, e.g. Salim.")
    add_voice_parser.add_argument(
        "--sample",
        action="append",
        required=True,
        help="Path to a voice sample file. Repeat for multiple files.",
    )

    voice_subparsers.add_parser("list", help="List local voice profiles.")

    add_run_arguments(parser)
    return parser


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text", default=None, help="Text to process through the MVP pipeline.")
    parser.add_argument("--source-language", default=None, help=f"Input {LANGUAGE_HINT}")
    parser.add_argument("--target-language", default=None, help=f"Output {LANGUAGE_HINT}")
    parser.add_argument("--json", action="store_true", help="Print raw JSON output only.")
    parser.add_argument("--no-speak", action="store_true", help="Do not speak the translated text aloud.")
    parser.add_argument(
        "--save-audio",
        default=None,
        help="Write the synthesized TTS audio to this file (e.g. out.mp3) so you can play it back.",
    )


async def process_text(
    text: str,
    source_language: str | None,
    target_language: str | None,
    save_audio: str | None = None,
    speak: bool = True,
) -> dict[str, object]:
    config = load_config()
    try:
        result = await run_pipeline(
            config=config,
            text=text,
            source_language=source_language,
            target_language=target_language,
            save_audio=save_audio,
            speak=speak,
        )
        output = asdict(result)
        output["provider_mode"] = config.mode
        output["fallback_reason"] = None
        return output
    except Exception as exc:
        if config.mode == "mock":
            raise

        fallback_config = PolyVoiceConfig(
            mode="mock",
            default_source_language=config.default_source_language,
            default_target_language=config.default_target_language,
            max_response_words=config.max_response_words,
            latency_target_ms=config.latency_target_ms,
        )
        result = await run_pipeline(
            config=fallback_config,
            text=text,
            source_language=source_language,
            target_language=target_language,
            save_audio=save_audio,
            speak=speak,
        )
        output = asdict(result)
        output["provider_mode"] = "mock"
        output["fallback_reason"] = f"real provider failed: {exc}"
        return output


async def run_pipeline(
    config: PolyVoiceConfig,
    text: str,
    source_language: str | None,
    target_language: str | None,
    save_audio: str | None,
    speak: bool,
):
    providers = build_provider_set(config, audio_output_path=save_audio, speak=speak)
    orchestrator = StreamingOrchestrator(
        config=config,
        stt=providers.stt,
        translator=providers.translator,
        conversation=providers.conversation,
        tts=providers.tts,
        audio_output=providers.audio_output,
    )
    return await orchestrator.process_text(
        text,
        source_language=source_language,
        target_language=target_language,
    )


def print_pipeline_result(
    result: dict[str, object],
    raw_json: bool = False,
    save_audio: str | None = None,
) -> None:
    if raw_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    print("\nPolyVoice pipeline result")
    print("-" * 26)
    print(f"English: {result['transcript']}")
    print(f"Yoruba: {result['input_translation']}")
    print(f"English response: {result['response']}")
    print(f"Yoruba response: {result['outbound_translation']}")
    print(f"Audio: {result['audio_bytes']} bytes at {result['audio_sample_rate']} Hz")
    if save_audio:
        print(f"Audio saved to: {save_audio}")
    if result.get("fallback_reason"):
        print(f"Provider fallback: {result['fallback_reason']}")
    print(f"Provider latency: {result['provider_latency_ms']:.2f} ms")
    print(f"Playback latency: {result['playback_latency_ms']:.2f} ms")
    print(f"Total latency: {result['total_latency_ms']:.2f} ms")
    print(f"Provider path within 200ms target: {result['within_target']}")
    print("Stage latency:")
    for stage, elapsed_ms in result["stage_latency_ms"].items():
        print(f"  {stage}: {elapsed_ms:.2f} ms")


def print_voice_profiles() -> None:
    profiles = list_voice_profiles()
    if not profiles:
        print("No local voice profiles yet. Add one with: python -m polyvoice.cli voice add --name Salim --sample C:\\path\\voice.wav")
        return

    print("\nLocal voice profiles")
    print("-" * 20)
    for profile in profiles:
        print(f"{profile.profile_id}: {profile.name} ({len(profile.samples)} sample(s), status={profile.status})")


def interactive_prompt() -> argparse.Namespace:
    print("\nPolyVoice MVP")
    print("1. Test text translation pipeline")
    print("2. Add my voice samples")
    print("3. List voice profiles")
    choice = input("Choose an option [1]: ").strip() or "1"

    if choice == "2":
        name = input("Voice profile name: ").strip()
        samples_text = input("Sample file path(s), separated by commas: ").strip()
        samples = [sample.strip().strip('"') for sample in samples_text.split(",") if sample.strip()]
        return argparse.Namespace(command="voice", voice_command="add", name=name, sample=samples)

    if choice == "3":
        return argparse.Namespace(command="voice", voice_command="list")

    text = input("Text to translate/respond to: ").strip()
    if not text:
        text = "Can we discuss pricing tomorrow?"
    target_language = input("Target language [yo]: ").strip() or "yo"
    return argparse.Namespace(
        command="run",
        text=text,
        source_language="en",
        target_language=target_language,
        json=False,
        no_speak=False,
        save_audio=None,
    )


async def run(args: argparse.Namespace) -> int:
    if args.command == "voice":
        if args.voice_command == "add":
            profile = create_voice_profile(args.name, args.sample)
            print(f"Created local voice profile '{profile.name}' with {len(profile.samples)} sample(s).")
            print("Next: connect an ElevenLabs voice adapter to upload this profile for cloning.")
            return 0
        if args.voice_command == "list":
            print_voice_profiles()
            return 0
        raise ValueError("Choose a voice command: add or list.")

    if not args.text:
        args = interactive_prompt()
        return await run(args)

    result = await process_text(
        args.text,
        args.source_language,
        args.target_language,
        save_audio=args.save_audio,
        speak=not args.no_speak,
    )
    print_pipeline_result(result, raw_json=args.json, save_audio=args.save_audio)
    return 0


def normalize_legacy_args(args: argparse.Namespace) -> argparse.Namespace:
    if args.command is None and args.text:
        args.command = "run"
    return args


def main() -> int:
    configure_console_output()
    parser = build_parser()
    try:
        args = normalize_legacy_args(parser.parse_args())
        if len(sys.argv) == 1:
            args = interactive_prompt()
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nPolyVoice stopped.")
        return 130
    except (OSError, ValueError) as exc:
        print(f"\nPolyVoice setup error: {exc}", file=sys.stderr)
        return 1


def configure_console_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
