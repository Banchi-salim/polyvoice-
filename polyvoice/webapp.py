from __future__ import annotations

import asyncio
import base64
import sys
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from polyvoice.bridge_runner import (
    BRIDGE_URL,
    get_bridge_status_async,
    list_bridge_chats_async,
    spawn_bridge as _spawn_bridge,
    terminate_bridge as _terminate_bridge,
)
from polyvoice.config import load_config
from polyvoice.conversations import ConversationStore, WhatsAppConversationService
from polyvoice.paths import reply_audio_dir, whatsapp_session_dir
from polyvoice.providers.whisper_stt import EmptyTranscriptError
from polyvoice.whatsapp import (
    WhatsAppClient,
    delete_contact_data,
    has_linked_session,
    parse_whatsapp_messages,
    write_reply_audio,
)


config = load_config()
store = ConversationStore()
conversation_service = WhatsAppConversationService(config, store)
whatsapp_client = WhatsAppClient(config)
app = FastAPI(title="PolyVoice WhatsApp Console")

# Static assets and Jinja templates live next to the webapp module in dev
# (`polyvoice/templates/`, `polyvoice/static/`) and inside the PyInstaller
# bundle at `sys._MEIPASS/polyvoice/...` in the frozen EXE. The conditional
# resolves the right base dir for both modes.
if getattr(sys, "frozen", False):
    _BASE = Path(getattr(sys, "_MEIPASS")) / "polyvoice"
else:
    _BASE = Path(__file__).resolve().parent
TEMPLATES_DIR = _BASE / "templates"
STATIC_DIR = _BASE / "static"

# Mounting the static directory before the catch-all `/` route means the
# browser can request `/static/app.css` and `/static/app.js` directly while
# the rest of the URL space is handled by the API endpoints below.
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class SimulatedTextMessage(BaseModel):
    contact_id: str = "demo-friend"
    contact_name: str = "Demo Friend"
    text: str
    source_language: str | None = None
    target_language: str | None = None


class WhatsAppWebBridgeMessage(BaseModel):
    contact_id: str
    contact_name: str
    kind: str = "text"
    text: str | None = None
    audio_base64: str | None = None
    mime_type: str | None = None
    source_language: str | None = None


class UserTextReply(BaseModel):
    contact_id: str
    contact_name: str
    text: str
    source_language: str | None = None
    target_language: str | None = None
    send_to_platform: bool = False


class UserLanguageSetting(BaseModel):
    language: str


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@app.get("/linking", response_class=HTMLResponse)
async def linking_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "linking.html")


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/conversations")
async def conversations() -> dict[str, object]:
    return {
        "contacts": store.contacts(),
        "messages": [asdict(message) for message in store.messages],
        "whatsapp_configured": whatsapp_client.configured,
        "provider_mode": config.mode,
        "user_language": conversation_service.user_language,
    }


@app.post("/api/settings/user-language")
async def set_user_language(setting: UserLanguageSetting) -> dict[str, object]:
    conversation_service.set_user_language(setting.language)
    return {"user_language": conversation_service.user_language}


@app.get("/api/whatsapp-web/status")
async def whatsapp_web_status() -> dict[str, object]:
    return await get_bridge_status_async()


@app.get("/api/whatsapp-web/chats")
async def whatsapp_web_chats() -> dict[str, object]:
    """Return the bridge's chat list for the contact picker.

    The bridge is the source of truth for real WhatsApp JIDs; without this
    the picker would only ever fall back to the ``demo-friend`` placeholder
    and every outbound message would be stored under that id (and rejected
    by the bridge because demo-friend is not a valid JID).
    """
    return await list_bridge_chats_async()


@app.get("/api/whatsapp-web/has-session")
async def whatsapp_web_has_session() -> dict[str, object]:
    """Return whether a `LocalAuth` session is on disk.

    Used by the linking page to decide whether to auto-start the bridge on
    load. Independent of the bridge process so the UI can still make the
    right choice when the bridge is crashed, restarting, or not yet up.
    """
    return {"linked": has_linked_session(whatsapp_session_dir())}


@app.post("/api/whatsapp-web/start")
async def start_whatsapp_web_bridge() -> dict[str, object]:
    status = await get_bridge_status_async()
    if status.get("running"):
        return status
    process = _spawn_bridge()
    return {"running": True, "status": "starting", "pid": process.pid if process else None}


@app.post("/api/whatsapp-web/stop")
async def stop_whatsapp_web_bridge() -> dict[str, object]:
    return await _terminate_bridge()


@app.post("/api/whatsapp-web/delink")
async def delink_whatsapp() -> dict[str, object]:
    """Disconnect the bridge and wipe all data tied to the linked contact.

    Captures the linked contact id from the bridge *before* tearing it down
    (the bridge status endpoint can no longer answer once we call disconnect),
    then removes the conversation rows, the OGG reply files, and the
    `LocalAuth` session directory for that contact.
    """
    pre_status = await get_bridge_status_async()
    contact_id = pre_status.get("me") if pre_status.get("running") else None

    await _terminate_bridge()

    deleted = {"conversations": 0, "audio_files": 0, "session": False}
    if contact_id:
        deleted = delete_contact_data(contact_id, store)

    return {"ok": True, "contact_id": contact_id, "deleted": deleted}


@app.get("/api/whatsapp-web/qr.svg")
async def whatsapp_web_qr_svg() -> Response:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            bridge_response = await client.get(f"{BRIDGE_URL}/qr.svg")
    except httpx.HTTPError:
        raise HTTPException(status_code=404, detail="WhatsApp Web bridge is not running.")
    return Response(
        content=bridge_response.content,
        status_code=bridge_response.status_code,
        media_type=bridge_response.headers.get("content-type", "image/svg+xml"),
    )


@app.get("/api/audio/{message_id}.ogg")
async def stream_reply_audio(message_id: str, request: Request) -> Response:
    """Stream a previously generated voice-note reply back to the browser.

    The message id is the persisted translation id written by the conversation
    service (e.g. ``demo-friend-2026-07-03T...-in``). Rather than re-deriving
    the on-disk filename from the URL (which only works if this endpoint's
    naming convention stays in lockstep with ``conversations.py``'s), we look
    up the message in the store and trust its ``reply_audio_path``. This also
    means requests for text-only messages (no ``reply_audio_path``) correctly
    404 instead of silently matching a stale or unrelated file.

    The endpoint advertises the file as ``audio/ogg; codecs=opus`` and supports
    HTTP range requests. The codec hint matters: Chromium-based browsers
    refuse to decode OGG/Opus when the Content-Type is the bare ``audio/ogg``,
    and the user sees a broken player with ``0:00 / 0:00``. Content-Length +
    range support let the browser seek and read metadata so the player shows
    a real duration. The TTS pipeline always emits 16 kHz mono Opus in an OGG
    container (see ``polyvoice.audio.transcode_to_ogg_opus``) so this is the
    correct container/codec pair to advertise.
    """
    message = next((m for m in store.messages if m.id == message_id), None)
    if message is None or not message.reply_audio_path:
        raise HTTPException(status_code=404, detail="Reply audio not found.")
    candidate = Path(message.reply_audio_path)
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Reply audio not found.")

    file_size = candidate.stat().st_size
    file_bytes = candidate.read_bytes()
    # The conversation service emits OGG/Opus at 16 kHz mono; advertise the
    # exact container/codec pair so Chromium's audio stack picks the right
    # decoder. ``audio/ogg`` alone is ambiguous and some browsers fall back
    # to "no duration known" without a codec hint.
    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": "audio/ogg; codecs=opus",
        "Cache-Control": "no-store",
    }

    # Honor HTTP range requests so the browser can seek and read just the
    # header chunk (which is all it needs for ``preload="metadata"``). The
    # request range is normally a single 0-N range; respond with 206 and a
    # Content-Range header. Multi-range responses add complexity for no real
    # benefit here, so we ignore them and serve the requested slice.
    range_header = request.headers.get("range") or request.headers.get("Range")
    if range_header:
        # Parse ``bytes=START-END``. END is optional (open-ended range).
        try:
            units, _, raw_range = range_header.partition("=")
            if units.strip().lower() != "bytes":
                raise ValueError("unsupported range unit")
            start_str, _, end_str = raw_range.partition("-")
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1
            if start < 0 or end >= file_size or start > end:
                raise ValueError("invalid range")
        except ValueError:
            # Per RFC 7233 the server may return 416 with a Content-Range
            # advertising the full size. We do that so the browser can
            # recover instead of falling into an infinite retry loop.
            return Response(
                status_code=416,
                headers={**base_headers, "Content-Range": f"bytes */{file_size}"},
            )
        slice_bytes = file_bytes[start : end + 1]
        return Response(
            content=slice_bytes,
            status_code=206,
            headers={
                **base_headers,
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(len(slice_bytes)),
            },
        )

    return Response(
        content=file_bytes,
        headers={**base_headers, "Content-Length": str(file_size)},
    )


@app.post("/api/simulate/text")
async def simulate_text(message: SimulatedTextMessage) -> dict[str, object]:
    turn = await conversation_service.handle_text(
        contact_id=message.contact_id,
        contact_name=message.contact_name,
        text=message.text,
        source_language=message.source_language,
    )
    return {"message": asdict(turn.message)}


@app.post("/api/reply/text")
async def reply_text(message: UserTextReply) -> dict[str, object]:
    turn = await conversation_service.handle_user_text_reply(
        contact_id=message.contact_id,
        contact_name=message.contact_name,
        text=message.text,
        source_language=message.source_language,
        target_language=message.target_language,
    )
    sent = False
    if message.send_to_platform and turn.translated_for_platform:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{BRIDGE_URL}/send",
                    json={
                        "to": message.contact_id,
                        "text": turn.translated_for_platform,
                    },
                )
                response.raise_for_status()
                sent = True
        except httpx.HTTPError as exc:
            if whatsapp_client.configured:
                try:
                    await whatsapp_client.send_text(message.contact_id, turn.translated_for_platform)
                    sent = True
                except Exception as cloud_exc:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Unable to send through WhatsApp bridge or Cloud API: {cloud_exc}",
                    ) from cloud_exc
            else:
                raise HTTPException(status_code=502, detail=f"Unable to send through WhatsApp bridge: {exc}") from exc
    return {
        "message": asdict(turn.message),
        "platform_text": turn.translated_for_platform,
        "platform_language": turn.platform_language,
        "sent": sent,
    }


@app.post("/api/reply/voice")
async def reply_voice(
    audio: UploadFile = File(...),
    contact_id: str = Form(...),
    contact_name: str = Form(...),
    source_language: str | None = Form(None),
    target_language: str | None = Form(None),
    send_to_platform: str = Form("false"),
) -> dict[str, object]:
    # FastAPI's `Form(bool)` coercion is finicky across versions and
    # multipart encoders — accept the value as a string and parse it here
    # so the call is robust to "true"/"1"/"yes"/"" variations.
    should_send = send_to_platform.strip().lower() in {"1", "true", "yes", "on"}
    content = await audio.read()
    try:
        turn = await conversation_service.handle_user_voice_reply(
            contact_id=contact_id,
            contact_name=contact_name,
            audio=content,
            mime_type=audio.content_type or "application/octet-stream",
            source_language=source_language,
            target_language=target_language,
        )
    except EmptyTranscriptError as exc:
        # Whisper ran successfully but heard nothing — the recording
        # was probably silence or mic noise. The frontend surfaces a
        # specific "I didn't catch that" toast and offers a re-record
        # button so the user can try again, instead of the generic
        # "transcription is offline" message.
        raise HTTPException(
            status_code=400,
            detail=f"voice_empty_transcript: {exc}",
        ) from exc
    except RuntimeError as exc:
        # STT (Whisper) couldn't be reached, the API rate-limited us, or
        # the fallback mock pipeline also failed. The frontend distinguishes
        # 502 from a generic 500 so the toast can say "voice transcription
        # is offline, try again or send as text" instead of an opaque
        # "Server returned 502".
        raise HTTPException(
            status_code=502,
            detail=f"voice_transcription_failed: {exc}",
        ) from exc
    sent = False
    sent_as_voice = False
    bridge_error: Exception | None = None
    if should_send and (turn.translated_for_platform or turn.audio):
        # Voice replies come back with TTS audio synthesized in the contact's
        # language. Prefer sending the audio (as a WhatsApp voice note) over
        # the text. If the audio is missing (TTS not configured, transcode
        # failed, or the user happened to record in the same language as the
        # contact), fall back to the translated text.
        if turn.audio and turn.audio_mime_type:
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.post(
                        f"{BRIDGE_URL}/send",
                        json={
                            "to": contact_id,
                            "audio_base64": base64.b64encode(turn.audio).decode("ascii"),
                            "mime_type": turn.audio_mime_type,
                            "as_voice": True,
                        },
                    )
                    response.raise_for_status()
                sent = True
                sent_as_voice = True
            except httpx.HTTPError as exc:
                # Audio send failed; the fallback below will try the text
                # path or raise if the bridge is the only option.
                bridge_error = exc
        if not sent and turn.translated_for_platform:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        f"{BRIDGE_URL}/send",
                        json={"to": contact_id, "text": turn.translated_for_platform},
                    )
                    response.raise_for_status()
                sent = True
                sent_as_voice = False
                bridge_error = None
            except httpx.HTTPError as exc:
                bridge_error = exc
        if not sent:
            # Last resort: the WhatsApp Cloud API. If that isn't configured
            # either, surface a 502 so the frontend can tell the user.
            if turn.translated_for_platform and whatsapp_client.configured:
                try:
                    await whatsapp_client.send_text(contact_id, turn.translated_for_platform)
                    sent = True
                    sent_as_voice = False
                except Exception as cloud_exc:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Unable to send through WhatsApp bridge or Cloud API: {cloud_exc}",
                    ) from cloud_exc
            else:
                raise HTTPException(
                    status_code=502,
                    detail=f"Unable to send through WhatsApp bridge: {bridge_error}",
                ) from bridge_error
    return {
        "message": asdict(turn.message),
        "platform_text": turn.translated_for_platform,
        "platform_language": turn.platform_language,
        "sent": sent,
        "sent_as_voice": sent_as_voice,
    }


@app.post("/api/simulate/voice")
async def simulate_voice(
    audio: UploadFile = File(...),
    contact_id: str = Form("demo-friend"),
    contact_name: str = Form("Demo Friend"),
    source_language: str | None = Form("fr"),
) -> dict[str, object]:
    content = await audio.read()
    turn = await conversation_service.handle_voice(
        contact_id=contact_id,
        contact_name=contact_name,
        audio=content,
        mime_type=audio.content_type or "application/octet-stream",
        source_language=source_language,
    )
    # Reuse the helper from `whatsapp.py` for backwards compatibility with
    # the original `.bin` dump path — the new canonical OGG file is also
    # written by the conversation service.
    legacy_path = reply_audio_dir() / f"{turn.message.id}.bin"
    write_reply_audio(legacy_path, turn.audio)
    return {
        "message": asdict(turn.message),
        "reply_audio_path": str(legacy_path),
        "reply_audio_mime_type": turn.audio_mime_type,
    }


@app.post("/bridges/whatsapp-web/message")
async def whatsapp_web_bridge_message(message: WhatsAppWebBridgeMessage) -> dict[str, object]:
    if message.kind == "audio":
        if not message.audio_base64:
            raise HTTPException(status_code=400, detail="audio_base64 is required for audio messages.")
        audio = base64.b64decode(message.audio_base64)

        # Pass source_language=None for auto-detection
        turn = await conversation_service.handle_voice(
            contact_id=message.contact_id,
            contact_name=message.contact_name,
            audio=audio,
            mime_type=message.mime_type or "application/octet-stream",
            source_language=None,  # Auto-detect
        )

        # Defensive check - ensure turn is not None
        if turn is None or turn.message is None:
            print(f"[polyvoice] Warning: handle_voice returned None or missing translated message")
            return {
                "translated_text": "Sorry, I couldn't process your voice message. Please try again.",
                "reply_audio_base64": None,
                "reply_audio_mime_type": None,
                "detected_language": "en",
                "send_reply": False,
            }

        return {
            "translated_text": turn.message.text,
            "original_text": turn.message.original_text,
            "reply_audio_base64": base64.b64encode(turn.audio).decode("ascii") if turn.audio else None,
            "reply_audio_mime_type": turn.audio_mime_type,
            "detected_language": turn.message.source_language,
            "send_reply": False,
        }

    if not message.text:
        raise HTTPException(status_code=400, detail="text is required for text messages.")

    # Use the provided language (detected by the bridge)
    turn = await conversation_service.handle_text(
        contact_id=message.contact_id,
        contact_name=message.contact_name,
        text=message.text,
        source_language=message.source_language,
    )

    # Defensive check - ensure turn is not None
    if turn is None or turn.message is None:
        print(f"[polyvoice] Warning: handle_text returned None or missing translated message")
        return {
            "translated_text": "Sorry, I couldn't process your message. Please try again.",
            "detected_language": "en",
            "send_reply": False,
        }

    return {
        "translated_text": turn.message.text,
        "original_text": turn.message.original_text,
        "detected_language": turn.message.source_language,
        "send_reply": False,
    }


@app.get("/webhooks/whatsapp")
async def verify_whatsapp_webhook(request: Request) -> PlainTextResponse:
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token and token == config.whatsapp_verify_token and challenge:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Invalid WhatsApp verify token.")


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request) -> JSONResponse:
    payload = await request.json()
    incoming_messages = parse_whatsapp_messages(payload)
    handled: list[dict[str, object]] = []
    for incoming in incoming_messages:
        if incoming.kind == "text" and incoming.text:
            turn = await conversation_service.handle_text(
                contact_id=incoming.sender_id,
                contact_name=incoming.sender_name,
                text=incoming.text,
                source_language=None,
            )
            handled.append({"message_id": incoming.message_id, "message": asdict(turn.message)})
        elif incoming.kind == "audio" and incoming.media_id:
            audio, mime_type = await whatsapp_client.download_media(incoming.media_id)
            turn = await conversation_service.handle_voice(
                contact_id=incoming.sender_id,
                contact_name=incoming.sender_name,
                audio=audio,
                mime_type=incoming.mime_type or mime_type,
                source_language=None,
            )
            handled.append({"message_id": incoming.message_id, "message": asdict(turn.message)})
    return JSONResponse({"ok": True, "handled": handled})


@app.on_event("startup")
async def _auto_connect_bridge() -> None:
    """Spawn the WhatsApp Web bridge on startup if a previous link is on disk.

    The user should never have to click "Connect" after a FastAPI restart if
    they already linked a phone in a previous session. We check for a
    non-empty `LocalAuth` directory and spawn the bridge in a background task
    so the FastAPI event loop isn't blocked on the Node process coming up.
    The 1.5s delay also gives the bridge port a moment to be free if a prior
    uvicorn shutdown left a TIME_WAIT socket.
    """
    if not has_linked_session(whatsapp_session_dir()):
        return

    async def _delayed_spawn() -> None:
        await asyncio.sleep(1.5)
        try:
            _spawn_bridge()
        except Exception as exc:  # pragma: no cover - log and move on
            print(f"[polyvoice] auto-connect bridge spawn failed: {exc!r}")

    asyncio.create_task(_delayed_spawn())
