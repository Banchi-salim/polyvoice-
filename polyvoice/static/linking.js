/* ------------------------------------------------------------------ *
 *  PolyVoice — linking page.                                          *
 *  Polls the bridge status, renders the QR for scanning, and offers   *
 *  a delink button that wipes the session and contact data.            *
 * ------------------------------------------------------------------ */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    card: $("linkingCard"),
    title: $("linkingTitle"),
    sub: $("linkingSub"),
    qrStage: $("qrStage"),
    qrImage: $("qrImage"),
    qrPlaceholder: $("qrPlaceholder"),
    qrPlaceholderText: $("qrPlaceholderText"),
    qrRefresh: $("qrRefresh"),
    ctaStage: $("ctaStage"),
    primaryBtn: $("primaryBtn"),
    delinkBtn: $("delinkBtn"),
    errorMsg: $("errorMsg"),
    statusChip: $("statusChip"),
    statusLabel: $("statusLabel"),
  };

  // The QR is a snapshot of the bridge's `state.qr` string. WhatsApp Web
  // rotates the QR on a timer, so we cache-bust the image every 8 s while
  // the linking page is in a `qr` state. The bridge itself will eventually
  // re-render the SVG with a fresh payload, so this is just belt-and-braces
  // in case the server caches the response.
  const QR_REFRESH_MS = 8000;
  let lastQrSrc = null;
  let qrRefreshTimer = null;

  // Watchdog: if the bridge stays in `starting` longer than this, surface
  // a hint that something is wrong (e.g. Puppeteer/Chromium failing to
  // launch). The QR will never appear in that case and the user needs to
  // know the bridge isn't silently working.
  const STARTING_TIMEOUT_MS = 20000;
  let startingSince = null;
  let startingWatchdog = null;

  // The page makes two HTTP calls on bootstrap: one to check whether a
  // session is already linked, and (if not) one to start the bridge. We
  // expose a `/api/whatsapp-web/has-session` endpoint in webapp.py for the
  // first probe so the auto-start decision doesn't depend on the bridge
  // being alive.
  let hasTriedAutoStart = false;

  function setCardState(state) {
    els.card.dataset.state = state;
  }

  function setError(message) {
    if (message) {
      els.errorMsg.textContent = message;
      els.errorMsg.hidden = false;
    } else {
      els.errorMsg.textContent = "";
      els.errorMsg.hidden = true;
    }
  }

  function showPrimary(label, action) {
    if (label) els.primaryBtn.textContent = label;
    els.primaryBtn.hidden = false;
    els.primaryBtn.dataset.action = action || "";
  }

  function hidePrimary() {
    els.primaryBtn.hidden = true;
    els.primaryBtn.removeAttribute("data-action");
  }

  function showDelink() {
    els.delinkBtn.hidden = false;
    els.delinkBtn.dataset.action = "delink";
  }

  function hideDelink() {
    els.delinkBtn.hidden = true;
    els.delinkBtn.removeAttribute("data-action");
  }

  function setBusy(busy) {
    els.primaryBtn.disabled = busy;
    els.delinkBtn.disabled = busy;
    els.qrRefresh.disabled = busy;
  }

  function setQrImage(url) {
    if (url === lastQrSrc) return;
    lastQrSrc = url;
    els.qrImage.src = url;
  }

  function clearQrImage() {
    lastQrSrc = null;
    els.qrImage.removeAttribute("src");
  }

  function startQrRefresh() {
    stopQrRefresh();
    qrRefreshTimer = setInterval(() => {
      // Re-bust the cache so the bridge can re-render a fresh QR.
      setQrImage(`/api/whatsapp-web/qr.svg?t=${Date.now()}`);
    }, QR_REFRESH_MS);
  }

  function stopQrRefresh() {
    if (qrRefreshTimer) {
      clearInterval(qrRefreshTimer);
      qrRefreshTimer = null;
    }
  }

  function showQrPanel(placeholderText) {
    els.qrStage.hidden = false;
    if (placeholderText) {
      els.qrPlaceholderText.textContent = placeholderText;
      els.qrPlaceholder.hidden = false;
      els.qrImage.style.display = "none";
    } else {
      els.qrPlaceholder.hidden = true;
      els.qrImage.style.display = "block";
    }
  }

  function hideQrPanel() {
    els.qrStage.hidden = true;
    clearQrImage();
    els.qrImage.style.display = "block";
    stopQrRefresh();
  }

  function startStartingWatchdog() {
    if (startingWatchdog) return;
    startingSince = Date.now();
    startingWatchdog = setTimeout(() => {
      // Bridge has been "starting" too long. Show the most recent error
      // in the panel so the user can see why the QR never appeared.
      setError(
        "The WhatsApp bridge is taking a long time to start. " +
        "Check the bridge log (data/whatsapp-web.err.log) for errors — " +
        "a common cause is Puppeteer/Chromium failing to launch."
      );
    }, STARTING_TIMEOUT_MS);
  }

  function stopStartingWatchdog() {
    if (startingWatchdog) {
      clearTimeout(startingWatchdog);
      startingWatchdog = null;
    }
    startingSince = null;
  }

  // ----- Status rendering -----

  function renderStatus(bridgeState) {
    const status = bridgeState.status || "stopped";
    const me = bridgeState.me;
    const lastError = bridgeState.lastError;
    const running = !!bridgeState.running;

    // Top status chip
    if (status === "ready") {
      els.statusChip.dataset.state = "ready";
    } else if (status === "qr" || status === "authenticated" || status === "starting" || status === "stopping") {
      els.statusChip.dataset.state = "qr";
    } else {
      els.statusChip.dataset.state = "loading";
    }
    els.statusLabel.textContent = me ? `Linked as ${me}` : statusLabelText(status);

    switch (status) {
      case "stopped": {
        setCardState("stopped");
        els.title.textContent = "Link WhatsApp";
        els.sub.textContent = running
          ? "Bridge is starting up…"
          : "Pair a phone to start translating messages automatically.";
        hideQrPanel();
        stopStartingWatchdog();
        if (running) {
          hidePrimary();
        } else {
          showPrimary("Start linking", "start");
        }
        hideDelink();
        setError(null);
        break;
      }
      case "starting": {
        setCardState("starting");
        els.title.textContent = "Starting bridge…";
        els.sub.textContent = "Waking up the WhatsApp Web bridge. The QR will appear as soon as the browser is ready.";
        showQrPanel("Starting bridge…");
        startStartingWatchdog();
        hidePrimary();
        hideDelink();
        setError(null);
        break;
      }
      case "qr": {
        setCardState("qr");
        els.title.textContent = "Scan with WhatsApp";
        els.sub.textContent = "Open the app and link a device to finish pairing.";
        setQrImage(`/api/whatsapp-web/qr.svg?t=${Date.now()}`);
        els.qrImage.style.display = "block";
        els.qrPlaceholder.hidden = true;
        els.qrStage.hidden = false;
        startQrRefresh();
        stopStartingWatchdog();
        hidePrimary();
        hideDelink();
        setError(null);
        break;
      }
      case "authenticated": {
        setCardState("authenticated");
        els.title.textContent = "Authenticating…";
        els.sub.textContent = "Almost there — finishing the handshake.";
        showQrPanel("Authenticating…");
        stopStartingWatchdog();
        hidePrimary();
        hideDelink();
        setError(null);
        break;
      }
      case "ready": {
        setCardState("ready");
        els.title.textContent = me ? `Linked as ${me}` : "Linked";
        const handled = bridgeState.handledCount || 0;
        els.sub.textContent = `${handled} message${handled === 1 ? "" : "s"} handled. You can now send and receive translated messages on WhatsApp.`;
        hideQrPanel();
        stopStartingWatchdog();
        hidePrimary();
        showDelink();
        setError(null);
        break;
      }
      case "stopping": {
        setCardState("stopping");
        els.title.textContent = "Disconnecting…";
        els.sub.textContent = "Closing the bridge.";
        showQrPanel("Disconnecting…");
        stopStartingWatchdog();
        hidePrimary();
        hideDelink();
        setError(null);
        break;
      }
      case "disconnected": {
        setCardState("disconnected");
        els.title.textContent = "Disconnected";
        els.sub.textContent = "The bridge stopped responding. Start a new link to reconnect.";
        showQrPanel("Bridge disconnected. Tap Reconnect.");
        stopStartingWatchdog();
        showPrimary("Reconnect", "start");
        hideDelink();
        setError(null);
        break;
      }
      case "auth_failure": {
        setCardState("auth_failure");
        els.title.textContent = "Authentication failed";
        els.sub.textContent = "The previous session was rejected. Restart the link to try again.";
        showQrPanel("Authentication failed.");
        stopStartingWatchdog();
        showPrimary("Try again", "start");
        hideDelink();
        setError(lastError || null);
        break;
      }
      default: {
        setCardState("loading");
        els.title.textContent = "Link WhatsApp";
        els.sub.textContent = `Bridge status: ${status}`;
        showQrPanel(`Bridge status: ${status}`);
        stopStartingWatchdog();
        hidePrimary();
        hideDelink();
        setError(null);
      }
    }
  }

  function statusLabelText(status) {
    return {
      stopped: "Not linked",
      starting: "Starting",
      qr: "Waiting for scan",
      authenticated: "Authenticating",
      ready: "Linked",
      stopping: "Stopping",
      disconnected: "Disconnected",
      auth_failure: "Auth failed",
    }[status] || status;
  }

  // ----- Network -----

  async function loadStatus() {
    try {
      const r = await fetch("/api/whatsapp-web/status", { cache: "no-store" });
      const data = await r.json();
      renderStatus(data);
      return data;
    } catch (err) {
      console.error("status poll failed", err);
      renderStatus({ status: "stopped", running: false });
      return null;
    }
  }

  async function startBridge() {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch("/api/whatsapp-web/start", { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        setError((data && data.detail) || "Could not start the bridge.");
      }
    } catch (err) {
      console.error("start failed", err);
      setError("Could not reach the server. Is uvicorn running?");
    } finally {
      setBusy(false);
    }
    await loadStatus();
  }

  async function delinkBridge() {
    const confirmed = window.confirm(
      "Delink this WhatsApp account? All messages, voice notes, and session data " +
      "for this phone will be permanently removed."
    );
    if (!confirmed) return;
    setBusy(true);
    setError(null);
    try {
      const r = await fetch("/api/whatsapp-web/delink", { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) {
        setError((data && data.detail) || "Delink failed.");
      } else {
        // Reset the auto-start flag so the freshly-delinked state can
        // immediately re-spawn the bridge and show a new QR.
        hasTriedAutoStart = false;
        // Kick off a fresh start right away so the user doesn't have to
        // click "Start linking" after delinking.
        await startBridge();
        return;
      }
    } catch (err) {
      console.error("delink failed", err);
      setError("Could not reach the server. Is uvicorn running?");
    } finally {
      setBusy(false);
    }
    await loadStatus();
  }

  // ----- Auto-start when no link exists -----

  async function maybeAutoStart() {
    if (hasTriedAutoStart) return;
    hasTriedAutoStart = true;

    // Probe the local file system to find out whether a previous link
    // exists. This is independent of the bridge process state, so we can
    // make the right decision even when the bridge is crashed.
    let linked = false;
    try {
      const r = await fetch("/api/whatsapp-web/has-session", { cache: "no-store" });
      if (r.ok) {
        const data = await r.json();
        linked = !!data.linked;
      }
    } catch (err) {
      console.warn("has-session probe failed", err);
    }

    if (linked) {
      // A session is on disk but the bridge may not be running. The status
      // poll will reflect that; if it shows stopped/disconnected, kick off
      // the bridge so the user lands on a connected state without a click.
      const status = await loadStatus();
      if (status && !status.running && status.status !== "ready") {
        await startBridge();
      }
      return;
    }

    // No link — start the bridge automatically so the QR appears without
    // the user having to click anything.
    await startBridge();
  }

  // ----- Wiring -----

  els.primaryBtn.addEventListener("click", () => {
    const action = els.primaryBtn.dataset.action;
    if (action === "start") startBridge();
  });

  els.delinkBtn.addEventListener("click", delinkBridge);

  els.qrRefresh.addEventListener("click", async () => {
    setQrImage(`/api/whatsapp-web/qr.svg?t=${Date.now()}`);
    await loadStatus();
  });

  function bootstrap() {
    loadStatus().then(maybeAutoStart);
    setInterval(loadStatus, 1500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
