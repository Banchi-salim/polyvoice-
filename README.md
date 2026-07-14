# PolyVoice — Realtime Translator

PolyVoice is a Windows desktop app that bridges the language gap between you
and the people you talk to on chat platforms (WhatsApp today; more in the
future). Connect WhatsApp, pick your language, and PolyVoice does the rest:

- **Incoming** — when a contact sends you a voice note or text in their
  language (Arabic, Korean, Yoruba, …), PolyVoice transcribes it, translates
  it into your selected language, and shows you a translated voice note or
  text in the app.
- **Outgoing** — when you reply by typing in your language (or by recording a
  voice note in your language), PolyVoice transcribes it, translates it into
  the contact's detected language, and sends the translated message through
  the connected chat platform.

The translation is bidirectional and works for any pair of languages the
underlying STT / translation / TTS providers support. PolyVoice never speaks
*for* you — it just makes what you already wrote understandable to the other
person, and what they wrote understandable to you.

## Quick Start (dev mode)

From this folder:

```powershell
cd "C:\Users\salim\PycharmProjects\polyvoice-ai-agent"
python -m polyvoice.launcher
```

That starts the FastAPI server, opens `http://127.0.0.1:8000` in your
default browser, and (if you've linked before) auto-reconnects the WhatsApp
bridge. To use the UI:

1. Pick your language in the composer dropdown (Igbo, Yoruba, Hausa, English,
   Arabic, Korean, French, Spanish — and any language the providers support).
2. Either connect WhatsApp (sidebar) and start receiving translated messages,
   or type a message into the composer to try the outgoing flow.
3. When the bridge is connected, replies you send through the composer are
   translated to the contact's detected language and delivered to them on
   WhatsApp.

## Run The WhatsApp Web Bridge

The WhatsApp Web bridge lets you use your personal WhatsApp account through
PolyVoice without a Meta Cloud API account. The first time:

1. Start the app (`python -m polyvoice.launcher`).
2. Open `http://127.0.0.1:8000`.
3. Click **Connect WhatsApp** in the sidebar.
4. Scan the QR code with your phone (WhatsApp → Settings → Linked devices).
5. The status chip turns green when the bridge is ready.

After that, every incoming voice note or text message gets translated into
your selected language automatically. The app remembers the link between
launches — you don't have to re-scan unless you explicitly delink.

### Environment

Copy `.env.example` to `.env` and fill in the API keys for the providers you
want to use. The two required pieces are:

- `GOOGLE_TRANSLATE_API_KEY` *or* `GOOGLE_APPLICATION_CREDENTIALS` — the
  translation engine.
- `DEEPGRAM_API_KEY` (or `OPENAI_API_KEY` for Whisper) — the speech-to-text
  engine.
- `ELEVENLABS_API_KEY` (optional) — the text-to-speech engine. Without it,
  PolyVoice still works in text-only mode.

`POLYVOICE_DEFAULT_TARGET_LANGUAGE` controls the language PolyVoice assumes
the *system user* speaks by default. The in-app language selector overrides
this at runtime.

## What PolyVoice Does (and Does Not Do)

- ✅ Reads incoming WhatsApp voice notes and text, transcribes them,
  translates them into your language, and shows the result in the app (with
  TTS audio when configured).
- ✅ Takes your reply (typed or voice), translates it into the contact's
  detected language, and sends it through WhatsApp.
- ✅ Auto-detects the contact's language on every new message.
- ✅ Remembers your selected language and your WhatsApp link between
  sessions.
- ❌ Does not reply to messages on your behalf. The composer is *yours*.
- ❌ Does not initiate conversations. You only see messages other people send
  you.
- ❌ Does not read or write to WhatsApp outside of the bridge script.

## Architecture

```text
                     +-----------------+
                     |   Chat app UI   |   <- composer + conversation view
                     +--------+--------+
                              |
                              v
                     +-----------------+
                     |   FastAPI app   |   <- polyvoice.webapp
                     +--------+--------+
                              |
              +---------------+---------------+
              |               |               |
              v               v               v
        +----------+   +-------------+   +---------+
        |   STT    |   |  Translate  |   |   TTS   |
        +----------+   +-------------+   +---------+
              |               |               |
              +-------+-------+-------+-------+
                              |
                              v
        +-----------------------------------------+
        |     WhatsApp Web bridge (Node)          |   <- scripts/whatsapp_web_bridge.js
        +-----------------------------------------+
                              |
                              v
                       WhatsApp network
```

Inbound flow:

```text
WhatsApp -> bridge -> /bridges/whatsapp-web/message
  -> STT (voice) -> translator (contact_lang -> your_lang) -> TTS -> conversation store
```

Outbound flow:

```text
Composer (text/voice) -> /api/reply/text | /api/reply/voice
  -> STT (your_lang) -> translator (your_lang -> contact_lang) -> bridge /send -> WhatsApp
```

## Build The Windows .exe

A single-folder PyInstaller build of the FastAPI app + Node WhatsApp Web
bridge. Python and Node are embedded — the end user doesn't need either
installed.

### Build (dev machine, one time per release)

```powershell
cd "C:\Users\salim\PycharmProjects\polyvoice-ai-agent"

# 1. Install PyInstaller into the dev env (one time).
python -m pip install pyinstaller

# 2. Stage the vendored bridge deps + portable Node.js into build/vendor/.
#    This step runs `npm ci` inside scripts/bridge/ and copies the
#    system node.exe next to the pruned node_modules.
python scripts\build_exe.py

# 3. Build the EXE.
pyinstaller --noconfirm PolyVoice.spec

# 4. Test it.
.\dist\PolyVoice\PolyVoice.exe
```

Output: `dist\PolyVoice\PolyVoice.exe` + `dist\PolyVoice\_internal\`. Zip the
whole `dist\PolyVoice\` folder and distribute.

### What is and isn't in the EXE

Bundled (read-only, extracted on first launch to
`%APPDATA%\PolyVoice\runtime\`):

- `polyvoice/templates/`, `polyvoice/static/`
- `scripts/whatsapp_web_bridge.js`, `scripts/bridge/package.json`
- `vendor/node.exe`, `vendor/node_modules/`

Deliberately excluded:

- `data/`, `node_modules/`, `.wwebjs_cache/`, `.env` — your current dev state
- `tests/`, `.idea/`, `.claude/`, `__pycache__/`, `scripts/data/`

### Runtime data location

Everything the app writes at runtime lives in `%APPDATA%\PolyVoice\`:

- `conversations.jsonl` — chat history
- `reply_audio/` — generated voice notes
- `voice_profiles/` — voice samples
- `whatsapp-web-session/` — `LocalAuth` credentials (your linked phone)
- `runtime/` — extracted Node.js, node_modules, and the bridge script
- `.env` — your API keys (read-only; place here or next to the EXE)
- `*.log` — bridge + uvicorn logs

The first time the EXE runs it copies the embedded Node.js, node_modules,
and the bridge script out of the bundle into the runtime directory. The
EXE never reads them from the project checkout or from the install folder,
so:

- **The EXE folder can be moved or copied freely.** `%APPDATA%` is the
  source of truth for runtime state, so the EXE behaves the same whether it
  lives in `C:\Program Files\PolyVoice\`, on a USB stick, or on a coworker's
  laptop. Just zip `dist\PolyVoice\` and send it.
- **The installer installs per-user** (`%LOCALAPPDATA%\Programs\PolyVoice`
  by default, not `C:\Program Files\…`), so no admin rights are required
  to install or run PolyVoice, and there's no risk of a system-wide DLL
  conflict.

### Notes

- First launch takes ~30s to extract the bridge. Subsequent launches: 2-3s.
- First WhatsApp scan downloads a Chromium browser (~150MB) into
  `%LOCALAPPDATA%\puppeteer\`. One-time.
- The Chromium download can fail in some sandboxed environments
  (pre-existing limitation). The UI still works for non-WhatsApp flows.
- Windows Defender may flag the unsigned EXE the first time. Add an
  exception or sign the binary before distribution.
