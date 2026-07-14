from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from polyvoice.paths import voice_profiles_dir

SUPPORTED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"}


def _voice_profile_dir() -> Path:
    """Call-time lookup so the value tracks the current data root."""
    return voice_profiles_dir()


@dataclass(frozen=True)
class VoiceSample:
    path: str
    bytes: int
    extension: str


@dataclass(frozen=True)
class VoiceProfile:
    name: str
    profile_id: str
    created_at: str
    samples: list[VoiceSample]
    provider_voice_id: str | None = None
    status: str = "local_samples_ready"


def slugify_name(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError("Voice profile name cannot be empty.")
    return slug


def validate_sample(path: str) -> VoiceSample:
    sample_path = Path(path).expanduser().resolve()
    if not sample_path.exists():
        raise ValueError(f"Voice sample not found: {sample_path}")
    if not sample_path.is_file():
        raise ValueError(f"Voice sample is not a file: {sample_path}")

    extension = sample_path.suffix.lower()
    if extension not in SUPPORTED_AUDIO_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
        raise ValueError(f"Unsupported sample type '{extension}'. Supported: {supported}")

    size = sample_path.stat().st_size
    if size < 10_000:
        raise ValueError("Voice sample looks too small. Use a clear recording with at least a few seconds of speech.")

    return VoiceSample(path=str(sample_path), bytes=size, extension=extension)


def create_voice_profile(
    name: str,
    sample_paths: list[str],
    output_dir: Path | None = None,
) -> VoiceProfile:
    if not sample_paths:
        raise ValueError("Add at least one voice sample file with --sample.")

    samples = [validate_sample(path) for path in sample_paths]
    profile_id = slugify_name(name)
    created_at = datetime.now(timezone.utc).isoformat()
    profile = VoiceProfile(
        name=name.strip(),
        profile_id=profile_id,
        created_at=created_at,
        samples=samples,
    )

    profile_dir = output_dir or _voice_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    output_path = profile_dir / f"{profile_id}.json"
    output_path.write_text(json.dumps(asdict(profile), indent=2), encoding="utf-8")
    return profile


def list_voice_profiles() -> list[VoiceProfile]:
    profile_dir = _voice_profile_dir()
    if not profile_dir.exists():
        return []

    profiles: list[VoiceProfile] = []
    for path in sorted(profile_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        samples = [VoiceSample(**sample) for sample in data.get("samples", [])]
        profiles.append(
            VoiceProfile(
                name=data["name"],
                profile_id=data["profile_id"],
                created_at=data["created_at"],
                samples=samples,
                provider_voice_id=data.get("provider_voice_id"),
                status=data.get("status", "local_samples_ready"),
            )
        )
    return profiles
