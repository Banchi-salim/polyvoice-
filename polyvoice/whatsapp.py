from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from polyvoice.config import PolyVoiceConfig
from polyvoice.paths import reply_audio_dir, whatsapp_session_dir

if TYPE_CHECKING:
    from polyvoice.conversations import ConversationStore


# Path the WhatsApp Web bridge uses for its `LocalAuth` session. The Node bridge
# is launched with `cwd=data_dir()`, so the relative path
# `./data/whatsapp-web-session` resolves under the configured data root
# (`./data` in dev, `%APPDATA%/PolyVoice` for the bundled Windows EXE).
# We expose call-time helpers rather than module-level Path constants so the
# values track the current `POLYVOICE_DATA_DIR` env var — important because
# PyInstaller-frozen mode and dev mode pick different defaults.



def has_linked_session(session_dir: Path | None = None) -> bool:
    """Return True if a previous WhatsApp `LocalAuth` session exists on disk.

    The bridge writes a non-empty directory tree once a phone has been linked
    (credentials, Chromium profile, etc.). A missing or empty directory means
    no link is active and the bridge should require a fresh QR scan.
    """
    if session_dir is None:
        session_dir = whatsapp_session_dir()
    if not session_dir.is_dir():
        return False
    try:
        next(session_dir.iterdir())
        return True
    except StopIteration:
        return False


def delete_contact_data(
    contact_id: str,
    store: "ConversationStore",
    *,
    session_dir: Path | None = None,
    audio_dir: Path | None = None,
) -> dict[str, object]:
    """Wipe everything tied to a single linked WhatsApp contact.

    Removes the in-memory `ConversationMessage` rows for `contact_id` and
    rewrites `conversations.jsonl` so the JSONL stays in sync, deletes the
    matching reply-audio OGG files (`<contact_id>-*.ogg`), and removes the
    `LocalAuth` session directory so the next bridge start forces a fresh QR
    scan. The `.wwebjs_cache/` browser profile is left alone — it's regenerated
    on the next launch and removing it just slows the next start up.

    Returns a summary of what was deleted.
    """
    if not contact_id:
        return {"conversations": 0, "audio_files": 0, "session": False}

    if session_dir is None:
        session_dir = whatsapp_session_dir()
    if audio_dir is None:
        audio_dir = reply_audio_dir()

    # 1. Filter the in-memory store and rewrite the JSONL.
    remaining = [m for m in store.messages if m.contact_id != contact_id]
    removed_count = len(store.messages) - len(remaining)
    store.messages = remaining
    try:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        with store.path.open("w", encoding="utf-8") as stream:
            for message in remaining:
                stream.write(json.dumps(asdict(message), ensure_ascii=False) + "\n")
    except OSError:
        # If the rewrite fails the in-memory list is still consistent for the
        # current process — the next successful `add()` will append to the
        # (possibly stale) JSONL and the operator can clean up by hand.
        pass

    # 2. Delete audio files prefixed with the contact id.
    audio_files_deleted = 0
    if audio_dir.is_dir():
        for audio_file in audio_dir.glob(f"{contact_id}-*.ogg"):
            try:
                audio_file.unlink()
                audio_files_deleted += 1
            except OSError:
                pass

    # 3. Remove the LocalAuth session directory.
    session_deleted = False
    if session_dir.is_dir():
        try:
            shutil.rmtree(session_dir)
            session_deleted = True
        except OSError:
            pass

    return {
        "conversations": removed_count,
        "audio_files": audio_files_deleted,
        "session": session_deleted,
    }


@dataclass(frozen=True)
class WhatsAppIncomingMessage:
    sender_id: str
    sender_name: str
    message_id: str
    kind: str
    text: str | None = None
    media_id: str | None = None
    mime_type: str | None = None


class WhatsAppClient:
    def __init__(self, config: PolyVoiceConfig) -> None:
        self.config = config
        self.base_url = f"https://graph.facebook.com/{config.whatsapp_api_version}"

    @property
    def configured(self) -> bool:
        return bool(self.config.whatsapp_access_token and self.config.whatsapp_phone_number_id)

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        if not self.config.whatsapp_access_token:
            raise RuntimeError("WHATSAPP_ACCESS_TOKEN is required to download WhatsApp media.")
        headers = {"Authorization": f"Bearer {self.config.whatsapp_access_token}"}
        async with httpx.AsyncClient(timeout=45.0) as client:
            metadata = await client.get(f"{self.base_url}/{media_id}", headers=headers)
            metadata.raise_for_status()
            payload = metadata.json()
            media_url = payload["url"]
            mime_type = payload.get("mime_type") or "application/octet-stream"
            media = await client.get(media_url, headers=headers)
            media.raise_for_status()
            return media.content, mime_type

    async def send_text(self, to: str, text: str) -> None:
        await self._send_message({"to": to, "type": "text", "text": {"body": text}})

    async def send_audio(self, to: str, audio: bytes, mime_type: str) -> None:
        media_id = await self._upload_media(audio, mime_type)
        await self._send_message({"to": to, "type": "audio", "audio": {"id": media_id}})

    async def _upload_media(self, audio: bytes, mime_type: str) -> str:
        if not self.config.whatsapp_access_token or not self.config.whatsapp_phone_number_id:
            raise RuntimeError("WhatsApp API credentials are not configured.")
        suffix = ".mp3" if mime_type == "audio/mpeg" else ".ogg"
        files = {
            "file": (f"polyvoice-reply{suffix}", audio, mime_type),
            "messaging_product": (None, "whatsapp"),
        }
        headers = {"Authorization": f"Bearer {self.config.whatsapp_access_token}"}
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                f"{self.base_url}/{self.config.whatsapp_phone_number_id}/media",
                headers=headers,
                files=files,
            )
            response.raise_for_status()
            return response.json()["id"]

    async def _send_message(self, message: dict[str, object]) -> None:
        if not self.config.whatsapp_access_token or not self.config.whatsapp_phone_number_id:
            return
        body = {"messaging_product": "whatsapp", **message}
        headers = {
            "Authorization": f"Bearer {self.config.whatsapp_access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/{self.config.whatsapp_phone_number_id}/messages",
                headers=headers,
                json=body,
            )
            response.raise_for_status()


def parse_whatsapp_messages(payload: dict[str, object]) -> list[WhatsAppIncomingMessage]:
    messages: list[WhatsAppIncomingMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = {
                contact.get("wa_id"): contact.get("profile", {}).get("name", contact.get("wa_id"))
                for contact in value.get("contacts", [])
            }
            for raw_message in value.get("messages", []):
                sender_id = raw_message.get("from", "unknown")
                message_type = raw_message.get("type")
                sender_name = contacts.get(sender_id, sender_id)
                if message_type == "text":
                    messages.append(
                        WhatsAppIncomingMessage(
                            sender_id=sender_id,
                            sender_name=sender_name,
                            message_id=raw_message.get("id", ""),
                            kind="text",
                            text=raw_message.get("text", {}).get("body", ""),
                        )
                    )
                elif message_type == "audio":
                    audio = raw_message.get("audio", {})
                    messages.append(
                        WhatsAppIncomingMessage(
                            sender_id=sender_id,
                            sender_name=sender_name,
                            message_id=raw_message.get("id", ""),
                            kind="audio",
                            media_id=audio.get("id"),
                            mime_type=audio.get("mime_type"),
                        )
                    )
    return messages


def write_reply_audio(path: Path, audio: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio)
