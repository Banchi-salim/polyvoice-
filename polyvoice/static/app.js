/* ------------------------------------------------------------------ *
 *  PolyVoice — client logic.                                          *
 *  Polls the FastAPI backend for conversations + bridge status, then  *
 *  renders a minimal chat UI. No frameworks — vanilla JS, ~150 lines. *
 * ------------------------------------------------------------------ */

(function () {
  "use strict";

  // ----- State -----
  const state = {
    selectedContact: "demo-friend",
    contacts: [],
    // Real WhatsApp JIDs from the linked bridge. We merge these into the
    // contact picker so the user can always pick a real contact instead of
    // the "demo-friend" placeholder. Without this, every outbound message
    // would be stored under contact_id="demo-friend" and the bridge would
    // refuse to deliver it.
    bridgeChats: [],
    messages: [],
    providerMode: "loading",
    whatsappConfigured: false,
    userLanguage: "ig",
  };
  // Ids of messages we've optimistically shown but that the server has
  // not yet confirmed. The 1.8s poll can fire while a send is in
  // flight; if we naively overwrite ``state.messages`` with the
  // server's response, the optimistic bubble (and the user's
  // recently-typed text) would *disappear* from the chat until the
  // next poll, and the user would have to refresh the page to see
  // their message land. The poll cycle now preserves any optimistic
  // message whose id isn't yet in the server's reply.
  const optimisticIds = new Set();
  // True from the moment the user hits Send until the server
  // response (or error) lands. The 1.8s poll checks this and skips
  // the re-render so a stale poll can't undo a fresh optimistic
  // bubble mid-send.
  let sendInFlight = false;
  const bridgeState = { running: false, status: "stopped", me: null, handledCount: 0 };
  const isMobile = () => window.matchMedia("(max-width: 767px)").matches;

  // ----- DOM refs -----
  const $ = (id) => document.getElementById(id);
  const els = {
    app: $("app"),
    sidebar: $("sidebar"),
    backdrop: $("backdrop"),
    backBtn: $("backBtn"),
    contacts: $("contacts"),
    title: $("title"),
    subtitle: $("subtitle"),
    messages: $("messages"),
    composer: $("composer"),
    nameInput: $("name"),
    textInput: $("text"),
    langSelect: $("language"),
    sendBtn: $("sendBtn"),
    statusChip: $("statusChip"),
    statusLabel: $("statusLabel"),
    bridgeDot: $("bridgeDot"),
    bridgeStatus: $("bridgeStatus"),
    bridgeSub: $("bridgeSub"),
    connectBtn: $("connectWhatsApp"),
    qrBox: $("qrBox"),
    qrImage: $("qrImage"),
    micBtn: $("micBtn"),
    micTimer: $("micTimer"),
    voicePreview: $("voicePreview"),
    voicePreviewAudio: $("voicePreviewAudio"),
    voicePreviewMeta: $("voicePreviewMeta"),
    voicePreviewDiscard: $("voicePreviewDiscard"),
    voicePreviewSend: $("voicePreviewSend"),
    toast: $("toast"),
  };

  // ----- Toast -----
  // Lightweight non-blocking surface for transient errors. Replaces
  // window.alert() so the chat doesn't get hijacked by a modal dialog
  // mid-recording. The optional `duration` overrides the default
  // kind-based timing (5s for errors, 2.8s for everything else) so
  // longer toasts (e.g. "voice transcription is offline") don't
  // disappear before the user can read them.
  let toastHideTimer = null;
  function showToast(message, kind, duration) {
    if (!els.toast) return;
    if (toastHideTimer) {
      clearTimeout(toastHideTimer);
      toastHideTimer = null;
    }
    els.toast.textContent = message;
    if (kind) {
      els.toast.dataset.kind = kind;
    } else {
      delete els.toast.dataset.kind;
    }
    els.toast.hidden = false;
    // Force a reflow so the transition runs even on rapid successive calls.
    void els.toast.offsetWidth;
    els.toast.classList.add("show");
    const ttl = typeof duration === "number" && duration > 0
      ? duration
      : (kind === "error" ? 5000 : 2800);
    toastHideTimer = setTimeout(() => {
      els.toast.classList.remove("show");
      setTimeout(() => {
        if (!els.toast.classList.contains("show")) els.toast.hidden = true;
      }, 200);
    }, ttl);
  }

  // ----- Voice recorder -----
  // The browser-side piece of the voice reply flow. We use MediaRecorder
  // against the user's microphone, hold the recorded blob until they tap
  // "Send voice" (or "Re-record"), then POST it to /api/reply/voice as
  // multipart/form-data. The backend's handle_user_voice_reply transcribes
  // the audio in the user's selected language, translates to the contact's
  // detected language, and either shows the translated message in the app
  // or sends it through the WhatsApp bridge (or both).
  const recorder = {
    stream: null,
    mediaRecorder: null,
    chunks: [],
    startedAt: 0,
    mimeType: "",
    blob: null,
    objectUrl: null,
    // Tracks the most recent outbound voice bubble so we can replace it
    // with the server's authoritative message after upload completes.
    optimisticId: null,
    timerInterval: null,
  };

  function formatDuration(ms) {
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}:${seconds.toString().padStart(2, "0")}`;
  }

  function startRecordingTimer() {
    if (recorder.timerInterval) clearInterval(recorder.timerInterval);
    const update = () => {
      if (els.micTimer) {
        els.micTimer.textContent = formatDuration(Date.now() - recorder.startedAt);
      }
    };
    update();
    recorder.timerInterval = setInterval(update, 250);
  }

  function stopRecordingTimer() {
    if (recorder.timerInterval) {
      clearInterval(recorder.timerInterval);
      recorder.timerInterval = null;
    }
  }

  // Pick a mime type the browser actually supports. The backend's
  // handle_user_voice_reply runs ffmpeg when the mime isn't ogg/opus, so
  // webm here is fine — it just costs a conversion pass.
  function pickRecorderMimeType() {
    const candidates = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/ogg",
      "audio/mp4",
    ];
    if (typeof MediaRecorder === "undefined") return null;
    for (const candidate of candidates) {
      if (MediaRecorder.isTypeSupported(candidate)) return candidate;
    }
    return ""; // empty string = let the browser pick its default
  }

  function isRecorderSupported() {
    return typeof navigator !== "undefined"
      && !!navigator.mediaDevices
      && typeof navigator.mediaDevices.getUserMedia === "function"
      && typeof MediaRecorder !== "undefined";
  }

  // Pre-warm the mic permission on page load (per the UX decision). If the
  // user declines we keep the mic button functional so they can try again,
  // but the browser will re-prompt only with an explicit click.
  function prewarmMicPermission() {
    if (!isRecorderSupported()) {
      if (els.micBtn) {
        els.micBtn.disabled = true;
        els.micBtn.title = "Voice replies are not supported in this browser.";
      }
      return;
    }
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
      navigator.mediaDevices
        .getUserMedia({ audio: true })
        .then((stream) => {
          // Don't keep the stream — we just want the permission grant.
          stream.getTracks().forEach((track) => track.stop());
        })
        .catch(() => {
          // User denied or browser blocked; they'll get a fresh prompt
          // when they click the mic button (if the browser allows it).
        });
    }
  }

  async function startRecording() {
    if (!isRecorderSupported()) return;
    if (recorder.mediaRecorder && recorder.mediaRecorder.state === "recording") return;

    try {
      recorder.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      console.error("mic permission denied", err);
      showToast("Microphone access was denied. Voice replies need mic permission.", "error");
      return;
    }

    recorder.mimeType = pickRecorderMimeType() || "";
    try {
      recorder.mediaRecorder = recorder.mimeType
        ? new MediaRecorder(recorder.stream, { mimeType: recorder.mimeType })
        : new MediaRecorder(recorder.stream);
    } catch (err) {
      console.error("MediaRecorder construction failed", err);
      recorder.stream.getTracks().forEach((track) => track.stop());
      recorder.stream = null;
      showToast("This browser couldn't start a voice recorder.", "error");
      return;
    }

    recorder.chunks = [];
    recorder.startedAt = Date.now();
    recorder.mediaRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) recorder.chunks.push(event.data);
    };
    recorder.mediaRecorder.onstop = () => {
      // Finalize the blob once the recorder fully stops, then drop the
      // mic stream — the browser shows a recording indicator while the
      // stream is live, and we don't want to leave it on after the user
      // is done recording.
      const actualMime = recorder.mediaRecorder.mimeType || recorder.mimeType || "audio/webm";
      recorder.blob = new Blob(recorder.chunks, { type: actualMime });
      if (recorder.stream) {
        recorder.stream.getTracks().forEach((track) => track.stop());
        recorder.stream = null;
      }
      stopRecordingTimer();
      showVoicePreview();
    };

    recorder.mediaRecorder.start();
    els.micBtn.dataset.state = "recording";
    els.micBtn.setAttribute("aria-label", "Stop recording");
    els.micBtn.title = "Tap to stop";
    startRecordingTimer();
  }

  function stopRecording() {
    if (!recorder.mediaRecorder || recorder.mediaRecorder.state !== "recording") return;
    recorder.mediaRecorder.stop();
    els.micBtn.dataset.state = "idle";
    els.micBtn.setAttribute("aria-label", "Record voice reply");
    els.micBtn.title = "Record voice";
    if (els.micTimer) els.micTimer.textContent = "0:00";
  }

  function showVoicePreview() {
    if (!recorder.blob) {
      hideVoicePreview();
      return;
    }
    if (recorder.objectUrl) URL.revokeObjectURL(recorder.objectUrl);
    recorder.objectUrl = URL.createObjectURL(recorder.blob);
    els.voicePreviewAudio.src = recorder.objectUrl;
    const seconds = (Date.now() - recorder.startedAt) / 1000;
    els.voicePreviewMeta.textContent = formatDuration(Date.now() - recorder.startedAt);
    els.voicePreview.hidden = false;
    // Disable the text send while previewing so the user doesn't double-send.
    els.sendBtn.disabled = true;
  }

  function hideVoicePreview() {
    if (recorder.objectUrl) {
      URL.revokeObjectURL(recorder.objectUrl);
      recorder.objectUrl = null;
    }
    if (els.voicePreviewAudio) {
      els.voicePreviewAudio.pause();
      els.voicePreviewAudio.removeAttribute("src");
      els.voicePreviewAudio.load();
    }
    if (els.voicePreview) els.voicePreview.hidden = true;
    els.sendBtn.disabled = false;
  }

  function discardRecording() {
    recorder.blob = null;
    recorder.chunks = [];
    hideVoicePreview();
  }

  async function sendRecording() {
    if (!recorder.blob) return;
    if (recorder.blob.size < 500) {
      showToast("That recording is too short to translate. Hold the mic a bit longer.", "error");
      return;
    }

    // Place an optimistic outbound voice bubble so the user sees immediate
    // feedback while the upload + translation run. The server response
    // replaces it with the authoritative message.
    const optimistic = appendOptimisticVoice(recorder.blob, recorder.mimeType || recorder.blob.type);
    els.voicePreviewSend.disabled = true;
    els.voicePreviewDiscard.disabled = true;
    sendInFlight = true;

    const form = new FormData();
    // The filename hint helps some browsers attach a sensible extension.
    const extension = (recorder.mimeType || recorder.blob.type || "audio/webm").includes("ogg") ? "ogg" : "webm";
    form.append("audio", recorder.blob, `voice-reply.${extension}`);
    form.append("contact_id", state.selectedContact);
    form.append("contact_name", els.nameInput.textContent.trim() || "Demo Friend");
    if (state.userLanguage) form.append("source_language", state.userLanguage);
    form.append(
      "send_to_platform",
      bridgeState.status === "ready" && state.selectedContact !== "demo-friend" ? "true" : "false",
    );

    try {
      const response = await fetch("/api/reply/voice", { method: "POST", body: form });
      if (!response.ok) {
        let bodyText = "";
        try { bodyText = await response.text(); } catch { /* ignore */ }
        throw new Error(`Server returned ${response.status}${bodyText ? `: ${bodyText}` : ""}`);
      }
      const data = await response.json();
      if (data && data.message) {
        replaceOptimistic(optimistic.id, data.message);
      }
      discardRecording();
      // Surface what actually happened on the platform side so the user
      // knows whether the message made it to WhatsApp.
      if (data && data.sent) {
        // Voice replies go out as voice notes (TTS audio in the contact's
        // language). If the audio path fell back to text, mention that.
        showToast(
          data.sent_as_voice ? "Sent as voice note." : "Sent as text (TTS unavailable).",
          "success",
        );
      } else if (bridgeState.status !== "ready") {
        showToast("Saved locally — connect WhatsApp to deliver replies.", "error");
      } else if (state.selectedContact === "demo-friend") {
        showToast("Saved locally — pick a real contact to deliver via WhatsApp.", "error");
      } else if (data && data.platform_text) {
        showToast("Translated, but the bridge didn't send. Check the bridge status.", "error");
      } else {
        showToast("Saved locally — couldn't detect a target language to translate into.", "error");
      }
      // Don't ``await loadState()`` here — the server already gave us
      // the authoritative message in ``data.message`` and
      // ``replaceOptimistic`` swapped the bubble in place. Re-running
      // the poll on top of an in-flight optimistic was the source of
      // the "won't send until I refresh" race: a fast 1.8s poll that
      // fired between the network call and the response persisting
      // would wipe the optimistic bubble. Let the next regular poll
      // tick reconcile.
    } catch (err) {
      console.error("voice send failed", err);
      // The server tags STT outages with a ``voice_transcription_failed``
      // prefix on the 502 body and *silent recordings* (Whisper returned
      // an empty transcript) with a ``voice_empty_transcript`` prefix on
      // a 400. Translate both into clear, distinct toasts so the user
      // knows whether the problem is the transcription service (DNS,
      // API outage, rate limit) or just that the recording didn't carry
      // any speech to translate.
      const message = String(err && err.message ? err.message : err);
      let toast = `Could not send the voice reply: ${message}`;
      let duration = 5000;
      if (message.includes("voice_empty_transcript")) {
        toast = "I didn't hear anything in that recording. Try again with a louder or longer message.";
        duration = 6000;
      } else if (message.includes("voice_transcription_failed")) {
        toast = "Voice transcription is offline right now. Try again in a moment, or send as text.";
        duration = 7000;
      } else if (message.includes("getaddrinfo") || message.includes("ConnectError")) {
        toast = "Couldn't reach the translation service. Check your network and try again.";
        duration = 7000;
      }
      showToast(toast, "error", duration);
      // Drop the optimistic bubble so the user can retry cleanly.
      optimisticIds.delete(optimistic.id);
      state.messages = state.messages.filter((m) => m.id !== optimistic.id);
      removeBubbleDom(optimistic.id);
      lastMessagesFingerprint = messagesFingerprint(state.messages);
      els.voicePreviewSend.disabled = false;
      els.voicePreviewDiscard.disabled = false;
    } finally {
      sendInFlight = false;
    }
  }

  function appendOptimisticVoice(blob, mimeType) {
    const now = new Date().toISOString();
    const id = `optimistic-voice-${now}`;
    const contactName = els.nameInput.textContent.trim() || "Demo Friend";
    const sourceLanguage = state.userLanguage || els.langSelect.value || "auto";
    const objectUrl = URL.createObjectURL(blob);
    const placeholder = {
      id,
      contact_id: state.selectedContact,
      contact_name: contactName,
      direction: "outbound",
      kind: "voice",
      language: sourceLanguage,
      // The translated text is unknown until the server replies; we keep
      // a small placeholder so the bubble layout is stable. The English
      // field doubles as the "what we said" preview line below the audio.
      text: "Voice reply...",
      english: "Voice reply...",
      timestamp: now,
      reply_audio_path: null,
      original_text: null,
      source_language: sourceLanguage,
      target_language: null,
      _local_audio_url: objectUrl,
    };
    state.messages.push(placeholder);
    optimisticIds.add(id);
    if (placeholder.contact_id === state.selectedContact) {
      appendBubbleDom(placeholder);
    }
    lastMessagesFingerprint = messagesFingerprint(state.messages);
    return placeholder;
  }

  // ----- Utilities -----
  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;",
    })[c]);
  }

  function formatTime(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      return d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
    } catch {
      return iso;
    }
  }

  function initialsFor(name) {
    if (!name) return "·";
    const parts = String(name).trim().split(/\s+/);
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  function audioUrlFor(message) {
    // The conversation service writes translated voice-note audio under
    // data/reply_audio/<id>.ogg. The webapp exposes it at /api/audio/<id>.ogg.
    // For optimistic voice bubbles the URL is a local blob: URL (set via
    // _local_audio_url on the placeholder object) — see appendOptimisticVoice.
    if (!message || message.kind !== "voice") return null;
    if (message._local_audio_url) return message._local_audio_url;
    if (!message.reply_audio_path) return null;
    // reply_audio_path looks like "data/reply_audio/<id>.ogg" — extract the id.
    const m = String(message.reply_audio_path).match(/([^/\\]+)\.ogg$/);
    if (!m) return null;
    // Encode the id so characters like `@` and `:` (the message id is a JID
    // plus an ISO timestamp, e.g. `234@c.us-2026-07-14T12:34:56-out`) are
    // treated as a single path segment by the server rather than getting
    // re-interpreted by a proxy or browser as userinfo/port.
    return `/api/audio/${encodeURIComponent(m[1])}.ogg`;
  }

  // ----- Rendering -----
  function renderContacts() {
    // Build the contact rows by merging two sources:
    //   1. state.contacts: the conversation store's view (per-contact snippet
    //      from the most recent message). Reflects chat history.
    //   2. state.bridgeChats: the live bridge's view of every JID the
    //      linked phone has ever seen. Reflects who we can actually deliver to.
    // Real bridge JIDs win because we need them to be the contact_id at
    // send time. The demo-friend placeholder is only shown when neither
    // source has any rows (i.e. before the user has linked WhatsApp).
    const byId = new Map();
    for (const c of state.bridgeChats) {
      if (!c || !c.id) continue;
      byId.set(c.id, {
        contact_id: c.id,
        contact_name: c.name || c.id,
        last_english: "",
        last_message: "",
        updated_at: c.timestamp ? new Date(c.timestamp * 1000).toISOString() : null,
        is_bridge: true,
      });
    }
    for (const c of state.contacts) {
      const existing = byId.get(c.contact_id);
      if (existing) {
        existing.last_english = existing.last_english || c.last_english || "";
        existing.last_message = existing.last_message || c.last_message || "";
        existing.updated_at = existing.updated_at || c.updated_at || null;
        if (c.contact_name && c.contact_name !== existing.contact_name && existing.contact_name === existing.contact_id) {
          existing.contact_name = c.contact_name;
        }
      } else {
        byId.set(c.contact_id, { ...c, is_bridge: false });
      }
    }
    let rows = Array.from(byId.values());
    if (!rows.length) {
      rows = [
        {
          contact_id: "demo-friend",
          contact_name: "Demo Friend",
          last_english: "Use the composer to send a first message.",
          updated_at: null,
          is_bridge: false,
        },
      ];
    }
    rows.sort((a, b) => {
      const aT = a.updated_at ? Date.parse(a.updated_at) || 0 : 0;
      const bT = b.updated_at ? Date.parse(b.updated_at) || 0 : 0;
      if (aT !== bT) return bT - aT;
      return String(a.contact_name).localeCompare(String(b.contact_name));
    });

    // If the previously-selected contact is gone (e.g. after delink), fall
    // back to the first real row so we don't leave the user on a stale
    // "demo-friend" id that no longer has a matching button.
    if (!rows.some((r) => r.contact_id === state.selectedContact)) {
      state.selectedContact = rows[0].contact_id;
    }

    // Reflect the active contact in the editable name field so the user can
    // see at a glance which contact they're typing into. (Previously this
    // field was a free-text "Demo Friend" label that misled users into
    // thinking they'd picked a real WhatsApp contact.)
    if (els.nameInput) {
      const active = rows.find((r) => r.contact_id === state.selectedContact);
      const nextName = (active && active.contact_name) || "Demo Friend";
      if (els.nameInput.textContent !== nextName) els.nameInput.textContent = nextName;
    }

    els.contacts.innerHTML = rows
      .map((c) => {
        const active = c.contact_id === state.selectedContact ? "active" : "";
        const snippet = escapeHtml(c.last_english || c.last_message || (c.is_bridge ? "WhatsApp contact" : ""));
        const time = c.updated_at ? formatTime(c.updated_at) : "";
        const bridgeBadge = c.is_bridge ? `<span class="contact-badge" aria-hidden="true">WA</span>` : "";
        return `
          <button class="contact ${active}" data-id="${escapeHtml(c.contact_id)}" type="button">
            <span class="avatar" aria-hidden="true">${escapeHtml(initialsFor(c.contact_name))}</span>
            <span class="contact-meta">
              <span class="contact-name">${escapeHtml(c.contact_name)}${bridgeBadge}</span>
              <span class="contact-snippet">${snippet}</span>
            </span>
            <span class="contact-time">${escapeHtml(time)}</span>
          </button>`;
      })
      .join("");

    els.contacts.querySelectorAll(".contact").forEach((button) => {
      button.addEventListener("click", () => {
        state.selectedContact = button.dataset.id;
        renderContacts();
        renderMessages();
        if (isMobile()) closeDrawer();
      });
    });
  }

  function bubbleHtml(m) {
    const speaker = m.direction === "inbound" ? "Friend" : "Me";
    const kindLabel = m.kind === "voice" ? "voice note" : "text";
    const audio = audioUrlFor(m);
    const original = m.original_text && m.original_text !== m.text
      ? m.original_text
      : (m.english && m.english !== m.text ? m.english : "");
    const translation =
      original
        ? `<div class="bubble-translation">${escapeHtml(original)}</div>`
        : "";
    const languageLabel = m.direction === "inbound"
      ? `${escapeHtml(m.source_language || "auto")} -> ${escapeHtml(m.language)}`
      : `${escapeHtml(m.language)} -> ${escapeHtml(m.target_language || "auto")}`;
    // ``preload="auto"`` (instead of ``preload="metadata"``) tells
    // the browser to fetch the whole small OGG/Opus file eagerly.
    // The TTS pipeline emits 5-20 KB voice notes, so the cost is
    // trivial, and reading the whole file is the only reliable way
    // to learn the duration: OGG stores the total granule position
    // in the *last* page, and ``preload="metadata"`` would have to
    // issue an extra range request to the end of the file to learn
    // it. We also tag the source as ``audio/ogg; codecs=opus`` so
    // Chromium picks the right decoder without sniffing the bytes.
    const audioBlock = audio
      ? `<div class="bubble-audio"><audio controls preload="auto" data-bubble-audio><source src="${audio}" type="audio/ogg; codecs=opus"></audio></div>`
      : "";
    return `
          <article class="bubble ${escapeHtml(m.direction)}" data-id="${escapeHtml(m.id)}">
            <div class="bubble-meta">
              <span>${speaker}</span>
              <span class="kind">${escapeHtml(kindLabel)}</span>
              <span>${languageLabel}</span>
            </div>
            ${audioBlock}
            <div class="bubble-text">${escapeHtml(m.text)}</div>
            ${translation}
            <div class="bubble-time">${escapeHtml(formatTime(m.timestamp))}</div>
          </article>`;
  }

  // Wire up the audio error handler for any new audio elements in `root`.
  // We use it from both the bulk render and the targeted append/replace
  // paths so a broken TTS file (or a 404 from the server endpoint) doesn't
  // leave a stuck ``0:00 / 0:00`` player on the bubble.
  function wireAudioErrorHandlers(root) {
    (root || els.messages).querySelectorAll("audio[data-bubble-audio]").forEach((audioEl) => {
      if (audioEl.dataset.errorWired === "1") return;
      audioEl.dataset.errorWired = "1";
      audioEl.addEventListener("error", () => {
        const wrap = audioEl.closest(".bubble-audio");
        if (wrap) wrap.remove();
      }, { once: true });
    });
  }

  // Append a single bubble for `message` to the DOM without re-rendering
  // the rest of the list. Used by the optimistic-update paths so a typing
  // user doesn't pay the cost of rebuilding every audio element on every
  // keystroke-bound send.
  function appendBubbleDom(message) {
    if (!els.messages) return;
    // Drop the "empty" placeholder if it's still there — the new bubble
    // is the first real message for this contact.
    const empty = els.messages.querySelector(".empty");
    if (empty) empty.remove();
    const wrap = document.createElement("div");
    wrap.innerHTML = bubbleHtml(message).trim();
    const node = wrap.firstElementChild;
    if (!node) return;
    els.messages.appendChild(node);
    wireAudioErrorHandlers(node);
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  // Replace the bubble with `id` in the DOM in place — preserves scroll
  // position, audio playback, and any other per-bubble state. Falls back
  // to a full re-render if the bubble can't be found (e.g. the user
  // switched contacts while the server response was in flight).
  function replaceBubbleDom(id, message) {
    if (!els.messages) return false;
    const existing = els.messages.querySelector(`.bubble[data-id="${cssEscape(id)}"]`);
    if (!existing) return false;
    const wrap = document.createElement("div");
    wrap.innerHTML = bubbleHtml(message).trim();
    const next = wrap.firstElementChild;
    if (!next) return false;
    existing.replaceWith(next);
    wireAudioErrorHandlers(next);
    return true;
  }

  // Remove a bubble by id (used when an optimistic send fails and we
  // want to drop the placeholder without a full re-render).
  function removeBubbleDom(id) {
    if (!els.messages) return;
    const existing = els.messages.querySelector(`.bubble[data-id="${cssEscape(id)}"]`);
    if (existing) existing.remove();
  }

  // CSS.escape polyfill — used to safely interpolate user-controlled
  // ids into selector strings. CSS.escape is available in every
  // browser this app targets, but the guard keeps the call site safe
  // in older WebViews.
  function cssEscape(value) {
    if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
      return CSS.escape(String(value));
    }
    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function renderMessages() {
    const list = state.messages.filter((m) => m.contact_id === state.selectedContact);
    const first = list[0];
    els.title.textContent = (first && first.contact_name) || "Realtime Translator";

    if (!list.length) {
      els.messages.innerHTML = `<div class="empty">No conversation yet. Connect WhatsApp or type a local reply draft below.</div>`;
      return;
    }

    els.messages.innerHTML = list.map(bubbleHtml).join("");

    // Drop any audio element whose source 404s or fails to decode so the
    // bubble doesn't show a broken `0:00 / 0:00` player. The optimized
    // case is the local-recording blob (always decodes), the fallback is
    // a server URL that might 404 if TTS/transcode failed.
    wireAudioErrorHandlers(els.messages);

    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function renderBridgeState() {
    const status = bridgeState.status || "stopped";
    els.bridgeDot.dataset.state = status;
    els.connectBtn.dataset.state = status;

    const labels = {
      stopped: "Not connected",
      starting: "Starting bridge…",
      qr: "Scan QR to connect",
      authenticated: "Authenticating…",
      ready: bridgeState.me ? `Connected as ${bridgeState.me}` : "Connected",
      stopping: "Disconnecting…",
      disconnected: "Disconnected",
      auth_failure: "Authentication failed",
    };
    const sub = {
      stopped: "Tap below to start",
      starting: "Waking up the bridge",
      qr: "Open WhatsApp → Linked devices",
      authenticated: "Almost there",
      ready: `${bridgeState.handledCount || 0} message${bridgeState.handledCount === 1 ? "" : "s"} handled`,
      stopping: "Closing the bridge",
      disconnected: "Tap to reconnect",
      auth_failure: "Tap to retry",
    };
    els.bridgeStatus.textContent = bridgeState.me ? "WhatsApp" : "WhatsApp";
    els.bridgeSub.textContent = sub[status] || status;

    // When the bridge is fully ready, the sidebar button becomes a navigation
    // shortcut to the dedicated /linking page where the delink action lives —
    // starting a second bridge from here would be redundant.
    if (status === "ready") {
      els.connectBtn.textContent = "Manage WhatsApp";
      els.connectBtn.dataset.action = "navigate";
    } else {
      els.connectBtn.textContent = "Connect WhatsApp";
      els.connectBtn.dataset.action = "start";
    }

    // Top status chip
    if (state.providerMode && state.providerMode !== "loading") {
      const wa = state.whatsappConfigured ? "WhatsApp configured" : "WhatsApp not configured";
      els.statusLabel.textContent = `${state.providerMode} · ${wa}`;
    }
    if (status === "ready") {
      els.statusChip.dataset.state = "ready";
    } else if (status === "qr" || status === "authenticated" || status === "starting" || status === "stopping") {
      els.statusChip.dataset.state = "qr";
    } else {
      els.statusChip.dataset.state = "loading";
    }

    // Only show the inline QR when the bridge is asking for a scan AND the
    // user hasn't already linked. Once `ready`, the QR is meaningless (the
    // session is already paired) and the sidebar's job is just navigation.
    if (status === "qr") {
      els.qrImage.src = `/api/whatsapp-web/qr.svg?t=${Date.now()}`;
      els.qrBox.hidden = false;
    } else {
      els.qrImage.removeAttribute("src");
      els.qrBox.hidden = true;
    }
  }

  // ----- Data loading -----
  // A stable fingerprint of the currently-rendered message list. We use it
  // in ``loadState`` to decide whether the poll cycle actually changed
  // anything worth re-rendering. Without this guard, the 1.8s poll
  // tore down and rebuilt every audio bubble on every tick — the browser
  // would start decoding the OGG/Opus stream, get halfway through
  // metadata, see the player replaced with a fresh element, and flash
  // ``0:00 / 0:02`` (or ``0:00 / 0:00``) back and forth. The user
  // reported this as "weird fluctuating behavior" while trying to play
  // the audio.
  //
  // The fingerprint is also updated by every code path that mutates
  // ``state.messages`` and re-renders (optimistic append / replace) so
  // the *next* ``loadState`` doesn't re-render the same content a second
  // time and tear the audio element down again.
  let lastMessagesFingerprint = "";
  function messagesFingerprint(messages) {
    // Concatenate the fields the UI actually shows. We deliberately omit
    // fields the render doesn't depend on (e.g. ``english`` is only used
    // as a fallback for ``original_text``, which is the field rendered).
    return (messages || [])
      .map((m) => [
        m.id || "",
        m.direction || "",
        m.kind || "",
        m.text || "",
        m.original_text || "",
        m.language || "",
        m.source_language || "",
        m.target_language || "",
        m.reply_audio_path || "",
        m.timestamp || "",
        m.contact_id || "",
        m.contact_name || "",
      ].join("|"))
      .join("\n");
  }

  // Update the cached fingerprint and re-render. Centralized so the
  // optimistic-append / replace / loadState paths all agree on when a
  // re-render is justified — call this instead of ``renderMessages()``
  // after mutating ``state.messages``.
  function renderMessagesAndTrack() {
    lastMessagesFingerprint = messagesFingerprint(state.messages);
    renderMessages();
  }

  async function loadState() {
    try {
      const r = await fetch("/api/conversations", { cache: "no-store" });
      const data = await r.json();
      const nextMessages = data.messages || [];
      // Re-render only when something the user can see has actually
      // changed. ``renderContacts`` is cheap so we still call it on every
      // poll — it can update snippet text and the active contact in
      // response to bridge state — but ``renderMessages`` is the one
      // that was destroying the audio player mid-decode.
      //
      // Critical merge step: if a send is in flight, the server's
      // response does not yet include the optimistic bubble, and a
      // naive ``state.messages = nextMessages`` would *delete* the
      // bubble from the UI even though the user just sent it. Keep
      // any optimistic message that isn't in the server reply until
      // the send completes (or fails). This is what fixed the
      // "won't send until I refresh" pattern: the 1.8s poll used to
      // wipe the in-flight bubble mid-send.
      const serverIds = new Set(nextMessages.map((m) => m.id));
      const pendingOptimistic = state.messages.filter(
        (m) => optimisticIds.has(m.id) && !serverIds.has(m.id),
      );
      const merged = pendingOptimistic.concat(nextMessages);
      const fingerprint = messagesFingerprint(merged);
      state.contacts = data.contacts || [];
      state.messages = merged;
      state.providerMode = data.provider_mode || "loading";
      state.whatsappConfigured = !!data.whatsapp_configured;
      state.userLanguage = data.user_language || state.userLanguage;
      if (els.langSelect.value !== state.userLanguage) {
        els.langSelect.value = state.userLanguage;
      }
      renderContacts();
      if (fingerprint !== lastMessagesFingerprint) {
        lastMessagesFingerprint = fingerprint;
        renderMessages();
      }
    } catch (err) {
      console.error("loadState failed", err);
    }
  }

  async function loadBridgeState() {
    try {
      const r = await fetch("/api/whatsapp-web/status", { cache: "no-store" });
      Object.assign(bridgeState, await r.json());
    } catch {
      bridgeState.running = false;
      bridgeState.status = "stopped";
    }
    if (bridgeState.status === "ready") {
      try {
        const r = await fetch("/api/whatsapp-web/chats", { cache: "no-store" });
        const data = await r.json();
        if (data && data.ok) state.bridgeChats = data.chats || [];
      } catch (err) {
        console.error("loadBridgeChats failed", err);
      }
    } else {
      // Don't keep stale chat rows around when the bridge is gone — the
      // contact list should reflect what we can actually deliver to.
      state.bridgeChats = [];
    }
    renderBridgeState();
    renderContacts();
    // Bridge polling never changes the message list itself — only the
    // contact list (and the status dot). Re-render messages only if the
    // selected contact changed (which already triggered a re-render in
    // the contact click handler); otherwise leave any playing audio
    // alone.
    const fingerprint = messagesFingerprint(state.messages);
    if (fingerprint !== lastMessagesFingerprint) {
      lastMessagesFingerprint = fingerprint;
      renderMessages();
    }
  }

  // ----- Drawer (mobile) -----
  function openDrawer() {
    els.sidebar.classList.add("open");
    els.backdrop.classList.add("show");
    els.backdrop.hidden = false;
  }
  function closeDrawer() {
    els.sidebar.classList.remove("open");
    els.backdrop.classList.remove("show");
    setTimeout(() => {
      if (!els.backdrop.classList.contains("show")) els.backdrop.hidden = true;
    }, 200);
  }

  // ----- Composer / send -----
  function appendOptimistic(text) {
    const now = new Date().toISOString();
    const id = `optimistic-${now}`;
    const contactName = els.nameInput.textContent.trim() || "Demo Friend";
    // The composer always represents the system user typing in *their own*
    // language, so the optimistic bubble can show the real source language
    // before the server response lands. The target language is "auto" until
    // the backend decides what the contact uses.
    const sourceLanguage = state.userLanguage || els.langSelect.value || "auto";
    const placeholder = {
      id,
      contact_id: state.selectedContact,
      contact_name: contactName,
      direction: "outbound",
      kind: "text",
      language: sourceLanguage,
      text,
      english: text,
      timestamp: now,
      reply_audio_path: null,
      original_text: null,
      source_language: sourceLanguage,
      target_language: null,
    };
    state.messages.push(placeholder);
    optimisticIds.add(id);
    // Targeted DOM append: the full re-render was rebuilding every
    // audio element on every send, which is what was making the
    // composer feel laggy on a long thread. The 1.8s poll's
    // optimistic-aware merge will reconcile state without a re-render.
    if (placeholder.contact_id === state.selectedContact) {
      appendBubbleDom(placeholder);
    }
    // Keep the fingerprint tracker in sync so the poll's fingerprint
    // check still triggers a re-render if *other* messages have
    // changed in the meantime.
    lastMessagesFingerprint = messagesFingerprint(state.messages);
    return id;
  }

  function replaceOptimistic(id, outbound) {
    const idx = state.messages.findIndex((m) => m.id === id);
    if (idx === -1) return;
    // Drop the optimistic local-recording blob URL so the bubble switches to
    // the server's TTS audio (the translated text read back in the contact's
    // language) once the response lands. Without this, `audioUrlFor` keeps
    // returning the placeholder URL and the user hears their own voice note
    // instead of the translated one.
    const { _local_audio_url: _ignored, ...placeholder } = state.messages[idx];
    if (_ignored) {
      try { URL.revokeObjectURL(_ignored); } catch { /* ignore */ }
    }
    const next = { ...placeholder, ...outbound };
    state.messages[idx] = next;
    optimisticIds.delete(id);
    if (next.contact_id === state.selectedContact) {
      // Targeted in-place replace so the playing audio (if any) and
      // scroll position are preserved. If the bubble isn't in the
      // current contact's view, fall back to a fingerprint-driven
      // re-render path.
      if (!replaceBubbleDom(id, next)) {
        lastMessagesFingerprint = messagesFingerprint(state.messages);
        renderMessages();
        return;
      }
    }
    lastMessagesFingerprint = messagesFingerprint(state.messages);
  }

  els.composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = els.textInput.value.trim();
    if (!text) return;
    // Clear the input *before* awaiting the network so the user can keep
    // typing the next message instead of waiting for this one to round
    // trip. The optimistic bubble already shows the text they sent.
    els.textInput.value = "";
    els.sendBtn.disabled = true;
    sendInFlight = true;
    const optimisticId = appendOptimistic(text);
    const body = {
      contact_id: state.selectedContact,
      contact_name: els.nameInput.textContent.trim() || "Demo Friend",
      text,
      source_language: els.langSelect.value || null,
      send_to_platform: bridgeState.status === "ready" && state.selectedContact !== "demo-friend",
    };
    try {
      const r = await fetch("/api/reply/text", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        throw new Error(`Server returned ${r.status}`);
      }
      const data = await r.json();
      if (data && data.message) replaceOptimistic(optimisticId, data.message);
      if (data && data.sent) {
        showToast("Sent to WhatsApp.", "success");
      } else if (bridgeState.status !== "ready") {
        showToast("Saved locally — connect WhatsApp to deliver replies.", "error");
      } else if (state.selectedContact === "demo-friend") {
        showToast("Saved locally — pick a real contact to deliver via WhatsApp.", "error");
      } else if (data && data.platform_text) {
        showToast("Translated, but the bridge didn't send. Check the bridge status.", "error");
      } else {
        showToast("Saved locally — couldn't detect a target language to translate into.", "error");
      }
      // The server already returned the authoritative message in
      // ``data.message`` and we replaced the optimistic bubble in
      // place. Re-running ``loadState`` here is the source of the
      // "won't send until I refresh" race: a fast 1.8s poll that
      // fires between this line and the response persisting can
      // wipe the bubble. ``loadState`` runs on its own cadence; let
      // it catch up naturally. If the user really wants an
      // immediate consistency check, the next 1.8s tick will do it.
    } catch (err) {
      console.error("send failed", err);
      showToast(`Could not send the reply: ${err.message || err}`, "error");
      // Drop the optimistic bubble so the user can retry cleanly.
      optimisticIds.delete(optimisticId);
      state.messages = state.messages.filter((m) => m.id !== optimisticId);
      removeBubbleDom(optimisticId);
      lastMessagesFingerprint = messagesFingerprint(state.messages);
    } finally {
      sendInFlight = false;
      els.sendBtn.disabled = false;
      els.textInput.focus();
    }
  });

  els.langSelect.addEventListener("change", async () => {
    const language = els.langSelect.value || "ig";
    state.userLanguage = language;
    try {
      await fetch("/api/settings/user-language", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ language }),
      });
    } catch (err) {
      console.error("language update failed", err);
    }
  });

  if (els.micBtn) {
    els.micBtn.addEventListener("click", async () => {
      const state2 = els.micBtn.dataset.state;
      if (state2 === "recording") {
        stopRecording();
      } else {
        // Hide the preview bar if a previous recording is still shown —
        // we want only one active recording at a time.
        if (recorder.blob) discardRecording();
        await startRecording();
      }
    });
  }
  if (els.voicePreviewDiscard) {
    els.voicePreviewDiscard.addEventListener("click", () => {
      discardRecording();
    });
  }
  if (els.voicePreviewSend) {
    els.voicePreviewSend.addEventListener("click", () => {
      sendRecording();
    });
  }

  els.connectBtn.addEventListener("click", async () => {
    const action = els.connectBtn.dataset.action;
    if (action === "navigate") {
      // Once linked, the sidebar button is a shortcut to the linking page
      // (where delink lives) rather than a start/stop toggle.
      window.location.href = "/linking";
      return;
    }
    const starting = action === "start";
    els.connectBtn.disabled = true;
    els.bridgeSub.textContent = starting ? "Starting…" : "Disconnecting…";
    try {
      await fetch(starting ? "/api/whatsapp-web/start" : "/api/whatsapp-web/stop", { method: "POST" });
    } catch (err) {
      console.error("bridge action failed", err);
    } finally {
      els.connectBtn.disabled = false;
    }
    await loadBridgeState();
  });

  els.backBtn.addEventListener("click", openDrawer);
  els.backdrop.addEventListener("click", closeDrawer);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && els.sidebar.classList.contains("open")) closeDrawer();
  });

  // ----- Bootstrap -----
  function bootstrap() {
    prewarmMicPermission();
    // Bridge first so the contact list has real JIDs available when
    // /api/conversations returns. The order matters: if conversations
    // arrives first with no rows, the picker falls back to the demo
    // placeholder; if the bridge chats arrive later the user has to wait
    // for a second poll to see their real contact list.
    loadBridgeState().then(() => {
      if (state.selectedContact === "demo-friend" && state.bridgeChats.length) {
        state.selectedContact = state.bridgeChats[0].id;
        renderContacts();
        renderMessagesAndTrack();
      }
    });
    loadState();
    setInterval(() => {
      loadState();
      loadBridgeState();
    }, 1800);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
