const express = require("express");
const QRCode = require("qrcode");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");

const POLYVOICE_URL = process.env.POLYVOICE_URL || "http://127.0.0.1:8000";
const BRIDGE_PORT = Number(process.env.POLYVOICE_WHATSAPP_WEB_PORT || 3030);
const REPLY_GROUPS = process.env.POLYVOICE_REPLY_GROUPS === "1";
const PRINT_TERMINAL_QR = process.env.POLYVOICE_PRINT_TERMINAL_QR === "1";
// Where whatsapp-web.js should write its LocalAuth session. The launcher
// sets this to the per-user data dir (e.g. %APPDATA%/PolyVoice/whatsapp-web-session)
// so the linked phone survives EXE moves and reinstalls. Falls back to the
// historical dev-mode relative path when unset.
const SESSION_DATA_PATH = process.env.POLYVOICE_WHATSAPP_SESSION_DIR || "./data/whatsapp-web-session";
let terminalQr;
if (PRINT_TERMINAL_QR) {
  terminalQr = require("qrcode-terminal");
}

const state = {
  status: "starting",
  qr: null,
  me: null,
  lastError: null,
  chatListReady: false,
  chatCount: 0,
  lastMessageAt: null,
  lastSyncAt: null,
  handledCount: 0,
};

const MAX_HANDLED_IDS = 5000;
const handledMessageIds = new Set();
const processingMessageIds = new Set();
let cachedChats = [];
let syncTimer = null;
let chatRefreshTimer = null;
let chatRefreshInFlight = false;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function markHandled(messageId) {
  handledMessageIds.add(messageId);
  if (handledMessageIds.size > MAX_HANDLED_IDS) {
    handledMessageIds.delete(handledMessageIds.values().next().value);
  }
}

const app = express();
app.use(express.json({ limit: "30mb" }));
app.use((req, res, next) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  next();
});

app.get("/status", (req, res) => {
  res.json(state);
});

// List every non-group chat the linked WhatsApp account has ever seen. Used
// by the FastAPI side to seed the contact picker so the user can pick a
// real JID before sending — without this, the picker would only ever show
// a single local placeholder row.
app.get("/chats", async (req, res) => {
  if (state.status !== "ready") {
    res.status(409).json({ ok: false, error: "WhatsApp bridge is not ready." });
    return;
  }
  try {
    const limit = Math.min(Math.max(Number(req.query.limit) || 50, 1), 200);
    if (!state.chatListReady) {
      await refreshChatCache();
    }
    res.json({ ok: true, chats: cachedChats.slice(0, limit), warming: !state.chatListReady });
  } catch (error) {
    recordError(error);
    res.json({
      ok: true,
      chats: cachedChats.slice(0, Math.min(Math.max(Number(req.query.limit) || 50, 1), 200)),
      warming: true,
      error: String(error && error.message ? error.message : error),
    });
  }
});

app.get("/qr", (req, res) => {
  res.json({ qr: state.qr, status: state.status });
});

app.get("/qr.svg", async (req, res) => {
  if (!state.qr) {
    res.status(404).type("text/plain").send(`No QR available. Current status: ${state.status}`);
    return;
  }
  const svg = await QRCode.toString(state.qr, {
    type: "svg",
    margin: 2,
    width: 320,
  });
  res.type("image/svg+xml").send(svg);
});

app.post("/disconnect", async (req, res) => {
  state.status = "stopping";
  state.qr = null;
  res.json({ ok: true, status: state.status });

  if (syncTimer) {
    clearInterval(syncTimer);
    syncTimer = null;
  }
  if (chatRefreshTimer) {
    clearInterval(chatRefreshTimer);
    chatRefreshTimer = null;
  }

  setTimeout(async () => {
    try {
      await client.destroy();
    } catch (error) {
      console.error("WhatsApp disconnect error:", error);
    } finally {
      process.exit(0);
    }
  }, 50);
});

app.post("/send", async (req, res) => {
  try {
    if (state.status !== "ready") {
      res.status(409).json({ ok: false, error: "WhatsApp bridge is not ready." });
      return;
    }
    const body = req.body || {};
    const to = body.to;
    if (!to) {
      res.status(400).json({ ok: false, error: "`to` is required." });
      return;
    }
    // Audio reply path — base64 audio + mime type, sent as a voice note
    // (the mic-bubble on the receiving phone) when as_voice is true.
    //
    // `Client.sendMessage` only takes three arguments (`chatId`, `content`,
    // `options`). Putting the flags in a fourth slot like the old
    // `sendMessage(to, media, undefined, { sendAudioAsVoice: true })` silently
    // dropped them on the floor — JS doesn't error on extra args, it just
    // never reads them — so the audio went out as a generic attachment
    // instead of a WhatsApp PPT (push-to-talk) voice note. Pass the options
    // object as the *third* argument so WA Web's internal `prepRawMedia`
    // actually receives `isPtt: true` and renders the mic-bubble on the
    // receiver's phone.
    if (body.audio_base64) {
      const mime = body.mime_type || "audio/ogg; codecs=opus";
      const asVoice = body.as_voice !== false;
      const media = new MessageMedia(mime, body.audio_base64, `polyvoice-voice.${mime.includes("ogg") ? "ogg" : "webm"}`);
      await client.sendMessage(to, media, { sendAudioAsVoice: !!asVoice });
      state.handledCount += 1;
      res.json({ ok: true, as_voice: !!asVoice });
      return;
    }
    // Plain text reply path.
    if (!body.text) {
      res.status(400).json({ ok: false, error: "Either `audio_base64` or `text` is required." });
      return;
    }
    await client.sendMessage(to, body.text);
    state.handledCount += 1;
    res.json({ ok: true, as_voice: false });
  } catch (error) {
    recordError(error);
    res.status(500).json({ ok: false, error: String(error && error.message ? error.message : error) });
  }
});

app.listen(BRIDGE_PORT, "127.0.0.1", () => {
  console.log(`PolyVoice WhatsApp Web bridge status: http://127.0.0.1:${BRIDGE_PORT}/status`);
});

const client = new Client({
  authStrategy: new LocalAuth({
    clientId: "polyvoice",
    dataPath: SESSION_DATA_PATH,
  }),
  puppeteer: {
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

client.on("qr", (qr) => {
  state.status = "qr";
  state.qr = qr;
  console.log(`WhatsApp QR ready: http://127.0.0.1:${BRIDGE_PORT}/qr.svg`);
  if (terminalQr) {
    terminalQr.generate(qr, { small: true });
  }
});

client.on("authenticated", () => {
  state.status = "authenticated";
  state.qr = null;
  console.log("WhatsApp Web authenticated.");
});

client.on("ready", () => {
  state.status = "ready";
  state.me = client.info && client.info.wid ? client.info.wid.user : null;
  console.log("WhatsApp Web bridge is ready.");
  refreshChatCache().catch(recordError);
  chatRefreshTimer = setInterval(() => {
    refreshChatCache().catch(recordError);
  }, 5000);
  syncUnreadChats().catch(recordError);
  syncTimer = setInterval(() => {
    syncUnreadChats().catch(recordError);
  }, 10000);
});

client.on("auth_failure", (message) => {
  state.status = "auth_failure";
  state.lastError = message;
  console.error("WhatsApp auth failure:", message);
});

client.on("disconnected", (reason) => {
  state.status = "disconnected";
  state.lastError = reason;
  console.error("WhatsApp disconnected:", reason);
});

client.on("message", async (message) => {
  await processIncomingMessage(message);
});

async function syncUnreadChats() {
  if (state.status !== "ready") return;
  state.lastSyncAt = new Date().toISOString();
  let chats;
  try {
    chats = await client.getChats();
  } catch (error) {
    recordError(error);
    return;
  }
  for (const chat of chats) {
    if (!chat.unreadCount) continue;
    if (!REPLY_GROUPS && chat.isGroup) continue;
    const messages = await chat.fetchMessages({
      limit: Math.min(Math.max(chat.unreadCount, 1), 10),
    });
    for (const message of messages) {
      await processIncomingMessage(message);
    }
  }
}

async function refreshChatCache() {
  if (state.status !== "ready" || chatRefreshInFlight) return cachedChats;
  chatRefreshInFlight = true;
  try {
    const chats = await client.getChats();
    const filtered = REPLY_GROUPS ? chats : chats.filter((c) => !c.isGroup);
    cachedChats = filtered
      .map((chat) => {
        const id = chat.id && chat.id._serialized ? chat.id._serialized : null;
        if (!id) return null;
        const contact = chat.contact || {};
        const name = chat.name || contact.pushname || contact.name || contact.number || id;
        return {
          id,
          name,
          is_group: !!chat.isGroup,
          unread_count: chat.unreadCount || 0,
          timestamp: chat.timestamp || null,
        };
      })
      .filter(Boolean)
      .sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
    state.chatListReady = true;
    state.chatCount = cachedChats.length;
    return cachedChats;
  } catch (error) {
    state.chatListReady = false;
    recordError(error);
    return cachedChats;
  } finally {
    chatRefreshInFlight = false;
  }
}

async function processIncomingMessage(message) {
  let messageId = null;
  try {
    if (message.fromMe) return;
    if (!REPLY_GROUPS && message.from.endsWith("@g.us")) return;
    messageId = message.id && message.id._serialized ? message.id._serialized : `${message.from}:${message.timestamp}:${message.body}`;
    if (handledMessageIds.has(messageId)) return;
    if (processingMessageIds.has(messageId)) return;
    processingMessageIds.add(messageId);

    const contact = await message.getContact();
    const contactName = contact.pushname || contact.name || contact.number || message.from;
    state.lastMessageAt = new Date().toISOString();

    if (message.hasMedia && (message.type === "audio" || message.type === "ptt")) {
      console.log(`Incoming WhatsApp voice note from ${contactName}; downloading media...`);
      const media = await downloadMediaWithRetry(message, messageId);
      if (!media || !media.data) {
        console.warn(`WhatsApp voice note media was not available after retries: ${messageId}`);
        return;
      }

      // Let Whisper auto-detect the language
      await askPolyVoice({
        contact_id: message.from,
        contact_name: contactName,
        kind: "audio",
        audio_base64: media.data,
        mime_type: media.mimetype,
        // source_language omitted = auto-detect
      });
      markHandled(messageId);
      state.handledCount += 1;
      return;
    }

    const text = (message.body || "").trim();
    if (!text) {
      markHandled(messageId);
      return;
    }

    // For text messages, let the PolyVoice backend detect the language
    await askPolyVoice({
      contact_id: message.from,
      contact_name: contactName,
      kind: "text",
      text,
      source_language: null, // backend will detect
    });
    markHandled(messageId);
    state.handledCount += 1;
  } catch (error) {
    recordError(error);
  } finally {
    if (messageId) {
      processingMessageIds.delete(messageId);
    }
  }
}

async function downloadMediaWithRetry(message, messageId) {
  const attempts = [0, 1000, 2000, 4000, 8000];
  let lastError = null;
  for (let index = 0; index < attempts.length; index += 1) {
    const delay = attempts[index];
    if (delay) {
      await sleep(delay);
    }
    try {
      const media = await message.downloadMedia();
      if (media && media.data) {
        if (index > 0) {
          console.log(`WhatsApp media became available after ${index + 1} attempts: ${messageId}`);
        }
        return media;
      }
      console.warn(`WhatsApp media download returned empty data on attempt ${index + 1}/${attempts.length}: ${messageId}`);
    } catch (error) {
      lastError = error;
      console.warn(`WhatsApp media download failed on attempt ${index + 1}/${attempts.length}:`, error);
    }
  }
  if (lastError) {
    throw lastError;
  }
  return null;
}

function recordError(error) {
  state.lastError = error && error.stack ? error.stack : String(error);
  console.error("WhatsApp bridge error:", error);
}

async function askPolyVoice(payload) {
  const response = await fetch(`${POLYVOICE_URL}/bridges/whatsapp-web/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`PolyVoice returned ${response.status}: ${await response.text()}`);
  }
  const reply = await response.json();
  console.log("PolyVoice reply:", JSON.stringify(reply));
  return reply;
}

client.initialize();
