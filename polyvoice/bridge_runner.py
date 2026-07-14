"""WhatsApp Web bridge process management.

Lives in its own module so the bundled EXE's launcher (which imports only
this — not the whole FastAPI app) can spawn the bridge at startup. The
FastAPI app also uses these helpers for the user-driven
``/api/whatsapp-web/{start,stop,delink}`` endpoints, so the spawn arguments
stay in lockstep between the two paths.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx

from polyvoice.paths import bridge_entry, data_dir, node_exe_path


BRIDGE_URL = os.getenv("POLYVOICE_WHATSAPP_WEB_URL", "http://127.0.0.1:3030")
# Module-level holder for the spawned subprocess. Exposed so the FastAPI app
# can also stop the bridge via the same handle the launcher created.
bridge_process: subprocess.Popen[str] | None = None


def spawn_bridge() -> subprocess.Popen[str] | None:
    """Start the Node WhatsApp Web bridge if one is not already running.

    Returns the running process, or ``None`` if a bridge is already up.
    The implementation is shared between the user-driven
    ``/api/whatsapp-web/start`` endpoint and the launcher's auto-spawn
    hook so the spawn arguments stay in lockstep.

    The Node binary resolves to the bundled ``node.exe`` under APPDATA when
    frozen, and to the ``node`` on PATH in dev. The bridge's working
    directory is the data root so the hardcoded
    ``./data/whatsapp-web-session`` path inside the bridge resolves under
    APPDATA for the EXE, and under ``./data`` for dev mode.
    """
    global bridge_process
    if bridge_process is not None and bridge_process.poll() is None:
        return bridge_process

    log_dir = data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("POLYVOICE_URL", "http://127.0.0.1:8000")
    env.setdefault("POLYVOICE_PRINT_TERMINAL_QR", "0")
    # Tell the bridge where to store its LocalAuth session. The bridge also
    # derives the same path from its cwd, but the env var makes the contract
    # explicit and survives any future cwd change.
    env.setdefault("POLYVOICE_WHATSAPP_SESSION_DIR", str(log_dir / "whatsapp-web-session"))
    env.setdefault("POLYVOICE_DATA_DIR", str(log_dir))
    env.setdefault("POLYVOICE_NODE_EXE", str(node_exe_path()))
    if node_exe_path().name.lower() == "node.exe":
        env.setdefault("NODE_PATH", str(node_exe_path().parent / "node_modules"))
    stdout = (log_dir / "whatsapp-web.out.log").open("a", encoding="utf-8")
    stderr = (log_dir / "whatsapp-web.err.log").open("a", encoding="utf-8")
    bridge_process = subprocess.Popen(
        [str(node_exe_path()), str(bridge_entry())],
        cwd=str(log_dir),
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    return bridge_process


async def get_bridge_status_async() -> dict[str, object]:
    """Fetch the bridge's ``/status`` payload, or a stopped-state stub.

    A connection error is interpreted as ``stopped``, not as a failure —
    the bridge takes a moment to come up, and the auto-connect logic
    polls this function until it reports ``ready``.
    """
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{BRIDGE_URL}/status")
            response.raise_for_status()
            payload = response.json()
            payload["running"] = True
            return payload
    except httpx.HTTPError:
        return {"running": False, "status": "stopped", "qr": None, "me": None, "lastError": None}


async def terminate_bridge() -> dict[str, object]:
    """Ask the bridge to disconnect, then make sure the process is gone.

    The bridge's ``/disconnect`` handler also calls ``client.destroy()`` and
    ``process.exit(0)``, so the process often terminates on its own; the
    ``terminate()``/kill fallback just guards against a bridge that hangs
    on shutdown.
    """
    global bridge_process
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{BRIDGE_URL}/disconnect")
    except httpx.HTTPError:
        pass

    if bridge_process is not None and bridge_process.poll() is None:
        bridge_process.terminate()
        try:
            bridge_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bridge_process.kill()
            bridge_process.wait(timeout=5)
    bridge_process = None
    return {"running": False, "status": "stopped", "qr": None, "me": None, "lastError": None}


async def list_bridge_chats_async(limit: int = 50) -> dict[str, object]:
    """Return the bridge's recent chats so the contact picker can show them.

    Used to seed the frontend with real WhatsApp JIDs (e.g.
    ``2348012345678@c.us``) so the user can pick a real contact instead of
    the demo placeholder. The bridge is the only place that knows the
    authoritative list, so we proxy through it.
    """
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(f"{BRIDGE_URL}/chats", params={"limit": limit})
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        return {"ok": False, "chats": [], "error": str(exc)}
