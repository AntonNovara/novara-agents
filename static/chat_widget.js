/*
 * NOVARA LANDING-CHAT-WIDGET
 * Einbettbar ohne Framework und ohne externe Abhängigkeiten -- eine einzige
 * <script>-Zeile genügt, der Rest läuft automatisch:
 *
 *   <script src="https://<dein-novara-agents-deployment>/static/chat_widget.js"></script>
 *
 * Spricht ausschließlich POST /api/v1/chat/landing an (agents/sdr_agent.py,
 * InboundChatGraph -- SDR-Agent im Inbound-Modus: beantwortet Fragen aus
 * novara_wissen.txt und qualifiziert den ICP-Fit über den Gesprächsverlauf).
 * Der Endpoint ist bewusst unauthentifiziert (siehe dessen Docstring in
 * main.py) -- ein Secret könnte in diesem Skript ohnehin nie verborgen
 * bleiben, es läuft im Browser jedes Besuchers.
 *
 * Konfiguration ausschließlich über data-*-Attribute auf dem eigenen
 * <script>-Tag (kein zweiter Script-Block nötig, siehe README unten):
 *   data-api-base      Basis-URL des Novara-Agents-Backends.
 *                       Default: der Origin, von dem DIESE Datei selbst
 *                       geladen wurde (funktioniert automatisch, wenn das
 *                       Backend auch /static/chat_widget.js ausliefert).
 *   data-title         Name im Chat-Header. Default "Novara Automation".
 *   data-greeting      Erste Bot-Nachricht beim ersten Öffnen.
 *   data-accent-color  Akzentfarbe (Bubble, Senden-Button, User-Bubbles).
 *                       Default "#0066FF" (Novara-Blau).
 *   data-avatar-src    Bild-URL für den Bubble-Avatar (Memoji o. ä.).
 *                       Default: <apiBase>/static/avatar.png. Existiert die
 *                       Datei nicht, zeigt die Bubble automatisch einen
 *                       Marken-Platzhalter (Gradient + "N") statt eines
 *                       kaputten Bild-Icons.
 *   data-visitor-name / data-visitor-email / data-visitor-company
 *                      Optional, wenn die Seite die Besucheridentität schon
 *                      kennt (z. B. eingeloggter Bereich) -- wird 1:1 als
 *                      visitor_info an den Endpoint durchgereicht.
 *
 * Datei-Anhänge (PDF/Foto): eine \u{1F4CE}-Büroklammer neben dem Eingabefeld
 * öffnet die native Dateiauswahl (accept="application/pdf,image/*"). Die
 * gewählte Datei wird per FileReader.readAsDataURL() im Browser zu Base64
 * kodiert und als "attachment": {filename, mime_type, content_base64} im
 * selben POST wie die nächste Chat-Nachricht mitgeschickt -- Gegenstück zu
 * main.py LandingAttachment / agents/sdr_agent.py InboundChatGraph.
 * document_node(). Client-seitig auf 8 MB begrenzt (dieselbe Grenze wie
 * document_node()s _MAX_ATTACHMENT_BYTES) und auf PDF/Bild-MIME-Typen
 * geprüft -- beides nur eine UX-Vorabprüfung, KEINE Sicherheitsgrenze
 * (das Skript läuft im Browser jedes Besuchers); document_node() prüft
 * Größe/Typ/Base64-Validität serverseitig ohnehin erneut. Ein Anhang ohne
 * Begleittext bekommt automatisch eine Standardnachricht, weil `message`
 * serverseitig ein Pflichtfeld ist (main.py LandingChatRequest).
 *
 * Bewusst KEIN Shadow DOM (Einfachheit/"liviano" vor Isolations-Härte) --
 * alle IDs/Klassen sind mit "novara-chat-" präfixiert, um Kollisionen mit
 * dem Rest der Seite unwahrscheinlich zu machen.
 */
(function () {
  "use strict";

  var CURRENT_SCRIPT = document.currentScript;
  var DATA = (CURRENT_SCRIPT && CURRENT_SCRIPT.dataset) || {};

  function scriptOrigin() {
    try {
      return new URL(CURRENT_SCRIPT.src).origin;
    } catch (e) {
      return "";
    }
  }

  var CONFIG = {
    apiBase: DATA.apiBase || scriptOrigin(),
    endpoint: "/api/v1/chat/landing",
    title: DATA.title || "Novara Automation",
    greeting: DATA.greeting || "Hallo! Wie kann ich dir bei der Automatisierung deines Betriebs helfen?",
    accentColor: DATA.accentColor || "#0066FF",
    avatarSrc: DATA.avatarSrc || "",
    visitorName: DATA.visitorName || "",
    visitorEmail: DATA.visitorEmail || "",
    visitorCompany: DATA.visitorCompany || "",
  };

  if (!CONFIG.apiBase) {
    // Kann passieren, wenn das Skript per document.write/eval nachgeladen
    // wurde (dann ist document.currentScript null) UND kein data-api-base
    // gesetzt ist -- ohne Basis-URL kann der Endpoint nicht erreicht
    // werden. Klarer Konsolenhinweis statt eines stillen No-ops.
    console.error(
      "[Novara Chat Widget] Keine API-Basis-URL ermittelbar. " +
        "Bitte data-api-base=\"https://...\" am <script>-Tag setzen."
    );
    return;
  }

  // Default erst HIER setzen, nicht in der CONFIG-Literal oben -- braucht
  // das bereits aufgelöste CONFIG.apiBase (avatar.png liegt im selben
  // /static-Verzeichnis wie diese Datei selbst, siehe main.py app.mount).
  if (!CONFIG.avatarSrc) {
    CONFIG.avatarSrc = CONFIG.apiBase + "/static/avatar.png";
  }

  // ── Storage (per-Browser, nicht per-Server) ─────────────────────────────

  var STORAGE_KEY_SESSION = "novara_chat_session_id";
  var STORAGE_KEY_HISTORY = "novara_chat_history";
  var MAX_STORED_TURNS = 40;

  function safeGet(key) {
    try {
      return localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function safeSet(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (e) {
      /* privater Modus, Speicher voll, o. ä. -- Widget bleibt trotzdem nutzbar */
    }
  }

  function uuid() {
    if (window.crypto && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  // session_id MUSS über die gesamte Konversation hinweg gleich bleiben --
  // der Server hält den Gesprächsverlauf/ICP-Zustand serverseitig unter
  // dieser ID (siehe agents/sdr_agent.py, InboundChatSession).
  var sessionId = safeGet(STORAGE_KEY_SESSION);
  if (!sessionId) {
    sessionId = uuid();
    safeSet(STORAGE_KEY_SESSION, sessionId);
  }

  var history = [];
  try {
    var rawHistory = safeGet(STORAGE_KEY_HISTORY);
    if (rawHistory) history = JSON.parse(rawHistory) || [];
  } catch (e) {
    history = [];
  }

  function persistHistory() {
    safeSet(STORAGE_KEY_HISTORY, JSON.stringify(history.slice(-MAX_STORED_TURNS)));
  }

  // ── Styles ───────────────────────────────────────────────────────────────

  var style = document.createElement("style");
  style.textContent =
    "#novara-chat-bubble{position:fixed;bottom:20px;right:20px;width:58px;height:58px;" +
    "border-radius:50%;background:" + CONFIG.accentColor + ";color:#fff;border:none;cursor:pointer;" +
    "box-shadow:0 4px 16px rgba(0,0,0,.3);font-size:26px;z-index:2147483000;overflow:hidden;" +
    "display:flex;align-items:center;justify-content:center;padding:0;transition:transform .15s ease;}" +
    "#novara-chat-bubble:hover{transform:scale(1.06);}" +
    // :not([hidden]) statt eines nackten Selektors -- sonst schlägt das hier
    // gesetzte display:block/flex (Autoren-Stylesheet) das UA-Default
    // [hidden]{display:none} IMMER, unabhängig von der Quellreihenfolge
    // (Autoren-Regeln schlagen User-Agent-Regeln bei gleicher Spezifität
    // immer, das ist keine reine Specificity-/Reihenfolge-Frage). Ohne das
    // bliebe ein per .hidden=true verstecktes Element trotzdem sichtbar.
    ".chat-avatar-img:not([hidden]){width:100%;height:100%;object-fit:cover;border-radius:50%;display:block;}" +
    "#novara-chat-avatar-fallback:not([hidden]){width:100%;height:100%;display:flex;align-items:center;" +
    "justify-content:center;background:linear-gradient(135deg,#0066FF,#C9A84C);color:#fff;" +
    "font-family:'Space Grotesk',-apple-system,sans-serif;font-weight:700;font-size:22px;}" +
    "#novara-chat-header-actions{display:flex;align-items:center;gap:10px;}" +
    "#novara-chat-clear{background:none;border:none;color:#F2F2F8;font-size:15px;cursor:pointer;" +
    "line-height:1;padding:0;opacity:.55;}" +
    "#novara-chat-clear:hover{opacity:1;}" +
    "#novara-chat-panel{position:fixed;bottom:90px;right:20px;width:350px;max-width:calc(100vw - 24px);" +
    "height:500px;max-height:calc(100vh - 130px);background:#fff;border-radius:14px;" +
    "box-shadow:0 12px 40px rgba(0,0,0,.3);display:none;flex-direction:column;overflow:hidden;" +
    "z-index:2147483000;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;}" +
    "#novara-chat-panel.novara-open{display:flex;}" +
    "#novara-chat-header{background:#0A0A0F;color:#F2F2F8;padding:14px 16px;font-weight:600;" +
    "display:flex;justify-content:space-between;align-items:center;font-size:15px;flex-shrink:0;}" +
    "#novara-chat-close{background:none;border:none;color:#F2F2F8;font-size:22px;cursor:pointer;" +
    "line-height:1;padding:0;opacity:.75;}" +
    "#novara-chat-close:hover{opacity:1;}" +
    "#novara-chat-messages{flex:1;overflow-y:auto;padding:12px;background:#f7f7fa;}" +
    ".novara-msg{max-width:85%;margin:6px 0;padding:9px 13px;border-radius:15px;font-size:14px;" +
    "line-height:1.45;white-space:pre-wrap;word-wrap:break-word;}" +
    ".novara-msg-user{background:" + CONFIG.accentColor + ";color:#fff;margin-left:auto;" +
    "border-bottom-right-radius:4px;}" +
    ".novara-msg-bot{background:#fff;color:#1a1a1a;border:1px solid #e5e7eb;margin-right:auto;" +
    "border-bottom-left-radius:4px;}" +
    ".novara-msg-typing{opacity:.55;font-style:italic;}" +
    "#novara-chat-cta{margin:0 12px 10px;padding:10px 12px;background:#fbf6e9;" +
    "border:1px solid #C9A84C;border-radius:10px;font-size:13px;line-height:1.4;color:#4a3f1f;}" +
    "#novara-chat-cta a{color:#8a6d1f;font-weight:700;text-decoration:none;}" +
    "#novara-chat-cta a:hover{text-decoration:underline;}" +
    "#novara-chat-input-row{display:flex;border-top:1px solid #e5e7eb;padding:8px;gap:8px;flex-shrink:0;}" +
    "#novara-chat-input{flex:1;border:1px solid #d1d5db;border-radius:20px;padding:9px 14px;" +
    "font-size:14px;outline:none;font-family:inherit;}" +
    "#novara-chat-input:focus{border-color:" + CONFIG.accentColor + ";}" +
    "#novara-chat-attach{background:none;border:none;color:#6b7280;font-size:19px;cursor:pointer;" +
    "flex-shrink:0;width:34px;height:38px;display:flex;align-items:center;justify-content:center;" +
    "border-radius:50%;transition:background .15s ease;}" +
    "#novara-chat-attach:hover{background:#f0f0f3;}" +
    "#novara-chat-attach:disabled{opacity:.4;cursor:default;}" +
    "#novara-chat-send{background:" + CONFIG.accentColor + ";color:#fff;border:none;border-radius:50%;" +
    "width:38px;height:38px;cursor:pointer;font-size:16px;flex-shrink:0;}" +
    "#novara-chat-send:disabled{opacity:.5;cursor:default;}" +
    "#novara-chat-attachment-chip{margin:0 12px 8px;padding:7px 10px;background:#f0f0f3;" +
    "border-radius:8px;font-size:12.5px;color:#333;display:none;align-items:center;gap:8px;}" +
    "#novara-chat-attachment-chip.novara-visible{display:flex;}" +
    "#novara-chat-attachment-chip .novara-attachment-name{flex:1;overflow:hidden;text-overflow:ellipsis;" +
    "white-space:nowrap;}" +
    "#novara-chat-attachment-remove{background:none;border:none;color:#6b7280;cursor:pointer;" +
    "font-size:15px;line-height:1;padding:0;flex-shrink:0;}" +
    "#novara-chat-attachment-remove:hover{color:#1a1a1a;}" +
    "@media (max-width:420px){#novara-chat-panel{right:12px;bottom:84px;}#novara-chat-bubble{right:12px;}}";
  document.head.appendChild(style);

  // ── DOM ──────────────────────────────────────────────────────────────────

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  var bubble = document.createElement("button");
  bubble.id = "novara-chat-bubble";
  bubble.type = "button";
  bubble.setAttribute("aria-label", "Chat öffnen");
  bubble.innerHTML =
    '<img src="' + escapeHtml(CONFIG.avatarSrc) + '" class="chat-avatar-img" alt="" />' +
    '<span id="novara-chat-avatar-fallback" hidden>N</span>';

  // avatar.png existiert (noch) nicht standardmäßig -- eleganter
  // Marken-Platzhalter (Gradient + Monogramm) statt eines kaputten
  // Bild-Icons, bis das echte Memoji-Asset unter static/avatar.png liegt.
  var avatarImg = bubble.querySelector(".chat-avatar-img");
  var avatarFallback = bubble.querySelector("#novara-chat-avatar-fallback");
  avatarImg.addEventListener(
    "error",
    function () {
      avatarImg.hidden = true;
      avatarFallback.hidden = false;
    },
    { once: true }
  );

  var panel = document.createElement("div");
  panel.id = "novara-chat-panel";
  panel.innerHTML =
    '<div id="novara-chat-header"><span>' + escapeHtml(CONFIG.title) + "</span>" +
    '<div id="novara-chat-header-actions">' +
    '<button id="novara-chat-clear" type="button" aria-label="Verlauf löschen" title="Verlauf löschen">\u{1F5D1}</button>' +
    '<button id="novara-chat-close" type="button" aria-label="Schließen">×</button>' +
    "</div></div>" +
    '<div id="novara-chat-messages"></div>' +
    '<div id="novara-chat-attachment-chip">' +
    '<span class="novara-attachment-name"></span>' +
    '<button id="novara-chat-attachment-remove" type="button" aria-label="Anhang entfernen">×</button>' +
    "</div>" +
    '<div id="novara-chat-input-row">' +
    '<input id="novara-chat-file-input" type="file" accept="application/pdf,image/*" hidden />' +
    '<button id="novara-chat-attach" type="button" aria-label="Datei anhängen" title="PDF oder Foto anhängen">\u{1F4CE}</button>' +
    '<input id="novara-chat-input" type="text" placeholder="Nachricht schreiben..." autocomplete="off" />' +
    '<button id="novara-chat-send" type="button" aria-label="Senden">➤</button>' +
    "</div>";

  document.body.appendChild(bubble);
  document.body.appendChild(panel);

  var messagesEl = panel.querySelector("#novara-chat-messages");
  var inputEl = panel.querySelector("#novara-chat-input");
  var sendBtn = panel.querySelector("#novara-chat-send");
  var closeBtn = panel.querySelector("#novara-chat-close");
  var clearBtn = panel.querySelector("#novara-chat-clear");
  var fileInputEl = panel.querySelector("#novara-chat-file-input");
  var attachBtn = panel.querySelector("#novara-chat-attach");
  var attachmentChipEl = panel.querySelector("#novara-chat-attachment-chip");
  var attachmentNameEl = attachmentChipEl.querySelector(".novara-attachment-name");
  var attachmentRemoveBtn = panel.querySelector("#novara-chat-attachment-remove");

  function renderMessage(role, text) {
    var div = document.createElement("div");
    div.className = "novara-msg " + (role === "user" ? "novara-msg-user" : "novara-msg-bot");
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function renderCta(bookingUrl) {
    var existing = document.getElementById("novara-chat-cta");
    if (existing) existing.remove();
    if (!bookingUrl) return;
    var div = document.createElement("div");
    div.id = "novara-chat-cta";
    var link = document.createElement("a");
    link.href = bookingUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "→ Kostenloses Erstgespräch buchen";
    div.appendChild(document.createTextNode("Klingt, als könnten wir dir helfen! "));
    div.appendChild(link);
    panel.insertBefore(div, panel.querySelector("#novara-chat-input-row"));
  }

  // Verlauf aus einer vorherigen Sitzung wiederherstellen; sonst Begrüßung zeigen.
  if (history.length > 0) {
    history.forEach(function (turn) {
      renderMessage(turn.role === "user" ? "user" : "bot", turn.content);
    });
  } else {
    renderMessage("bot", CONFIG.greeting);
  }

  var isOpen = false;
  function setOpen(next) {
    isOpen = next;
    panel.classList.toggle("novara-open", isOpen);
    if (isOpen) inputEl.focus();
  }

  bubble.addEventListener("click", function () {
    setOpen(!isOpen);
  });
  closeBtn.addEventListener("click", function () {
    setOpen(false);
  });

  // Verlauf/Session zurücksetzen -- für Tests ("von vorn anfangen") oder
  // falls ein Besucher selbst neu beginnen möchte. Setzt bewusst eine NEUE
  // session_id: der Server hält ICP-Score/Gesprächsverlauf unter der alten
  // ID weiter vor (InboundChatSession in agents/sdr_agent.py), verwaist
  // dort harmlos statt aktiv gelöscht zu werden -- die alte Session fällt
  // irgendwann unter die Verdrängung durch _MAX_INBOUND_SESSIONS.
  function resetSession() {
    history = [];
    messagesEl.innerHTML = "";
    var existingCta = document.getElementById("novara-chat-cta");
    if (existingCta) existingCta.remove();
    clearPendingAttachment();
    sessionId = uuid();
    safeSet(STORAGE_KEY_SESSION, sessionId);
    safeSet(STORAGE_KEY_HISTORY, "[]");
    renderMessage("bot", CONFIG.greeting);
  }

  clearBtn.addEventListener("click", function () {
    if (window.confirm("Verlauf wirklich löschen und neu starten?")) {
      resetSession();
    }
  });

  // ── Anhänge (PDF/Foto) ───────────────────────────────────────────────────
  // Gegenstück zu main.py LandingAttachment + agents/sdr_agent.py
  // InboundChatGraph.document_node(): filename/mime_type/content_base64,
  // dieselbe Größengrenze (8 MB Rohbytes) wie document_node()s
  // _MAX_ATTACHMENT_BYTES -- Client-seitig geprüft, damit ein zu großer
  // Upload gar nicht erst den Server-Roundtrip verbraucht, ändert aber
  // nichts an der serverseitigen Prüfung (das Widget läuft im Browser
  // jedes Besuchers, ein Client-Check ist nie die Sicherheitsgrenze).
  var MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;
  var pendingAttachment = null; // {filename, mime_type, content_base64} oder null

  function isSupportedAttachmentType(file) {
    var type = (file.type || "").toLowerCase();
    if (type === "application/pdf") return true;
    if (type.indexOf("image/") === 0) return true;
    // Manche Browser/Betriebssysteme setzen file.type nicht zuverlässig --
    // Dateiendung als Fallback, exakt dieselbe PDF-Erkennung wie
    // document_node() serverseitig (mime_type ODER .pdf-Dateiname).
    return !type && /\.pdf$/i.test(file.name || "");
  }

  function renderAttachmentChip() {
    if (!pendingAttachment) {
      attachmentChipEl.classList.remove("novara-visible");
      attachmentNameEl.textContent = "";
      return;
    }
    attachmentNameEl.textContent = "\u{1F4CE} " + pendingAttachment.filename;
    attachmentChipEl.classList.add("novara-visible");
  }

  function clearPendingAttachment() {
    pendingAttachment = null;
    fileInputEl.value = "";
    renderAttachmentChip();
  }

  function handleFileSelected(file) {
    if (!file) return;

    if (!isSupportedAttachmentType(file)) {
      renderMessage("bot", "Dieser Dateityp wird nicht unterstützt. Bitte ein PDF oder ein Foto (JPG/PNG) anhängen.");
      fileInputEl.value = "";
      return;
    }
    if (file.size > MAX_ATTACHMENT_BYTES) {
      renderMessage("bot", "Die Datei ist zu groß (max. 8 MB). Bitte eine kleinere Datei anhängen.");
      fileInputEl.value = "";
      return;
    }

    var reader = new FileReader();
    reader.onload = function () {
      // readAsDataURL liefert "data:<mime>;base64,<payload>" -- main.py
      // LandingAttachment.content_base64 erwartet NUR den Payload-Teil.
      var result = String(reader.result || "");
      var commaIndex = result.indexOf(",");
      var base64Payload = commaIndex >= 0 ? result.slice(commaIndex + 1) : "";
      if (!base64Payload) {
        renderMessage("bot", "Die Datei konnte nicht gelesen werden. Bitte versuch es erneut.");
        return;
      }
      pendingAttachment = {
        filename: file.name || "Anhang",
        mime_type: file.type || (/\.pdf$/i.test(file.name || "") ? "application/pdf" : ""),
        content_base64: base64Payload,
      };
      renderAttachmentChip();
      inputEl.focus();
    };
    reader.onerror = function () {
      renderMessage("bot", "Die Datei konnte nicht gelesen werden. Bitte versuch es erneut.");
    };
    reader.readAsDataURL(file);
  }

  attachBtn.addEventListener("click", function () {
    fileInputEl.click();
  });
  fileInputEl.addEventListener("change", function () {
    handleFileSelected(fileInputEl.files && fileInputEl.files[0]);
  });
  attachmentRemoveBtn.addEventListener("click", function () {
    clearPendingAttachment();
  });

  // ── Networking ───────────────────────────────────────────────────────────

  var sending = false;

  function sendMessage() {
    var text = inputEl.value.trim();
    var attachmentToSend = pendingAttachment;
    if (!text && !attachmentToSend) return;
    if (sending) return;

    // message ist serverseitig ein Pflichtfeld (main.py LandingChatRequest,
    // min_length=1) -- bei einem reinen Datei-Upload ohne Begleittext einen
    // sinnvollen Default mitschicken statt eine leere Nachricht zu senden.
    var messageToSend = text || ("Ich habe eine Datei angehängt: " + attachmentToSend.filename);
    var displayText = attachmentToSend
      ? (text ? text + "\n\u{1F4CE} " + attachmentToSend.filename : "\u{1F4CE} " + attachmentToSend.filename)
      : text;

    renderMessage("user", displayText);
    history.push({ role: "user", content: displayText });
    persistHistory();
    inputEl.value = "";
    clearPendingAttachment();

    sending = true;
    sendBtn.disabled = true;
    attachBtn.disabled = true;
    var typingEl = renderMessage("bot", "…");
    typingEl.classList.add("novara-msg-typing");

    fetch(CONFIG.apiBase + CONFIG.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        message: messageToSend,
        visitor_info: {
          name: CONFIG.visitorName,
          email: CONFIG.visitorEmail,
          company: CONFIG.visitorCompany,
          phone: "",
        },
        // Gegenstück zu main.py LandingAttachment -- null, wenn kein Anhang
        // gewählt wurde (Optional-Feld, Pydantic akzeptiert null wie ein
        // fehlendes Feld).
        attachment: attachmentToSend
          ? {
              filename: attachmentToSend.filename,
              mime_type: attachmentToSend.mime_type,
              content_base64: attachmentToSend.content_base64,
            }
          : null,
      }),
    })
      .then(function (res) {
        return res.json().then(function (data) {
          return { ok: res.ok, data: data };
        });
      })
      .then(function (result) {
        typingEl.remove();
        var data = result.data || {};
        if (!result.ok || !data.success) {
          renderMessage(
            "bot",
            "Entschuldigung, deine Nachricht konnte gerade nicht verarbeitet werden. Bitte versuch es später erneut."
          );
          return;
        }
        var reply = data.reply || "…";
        renderMessage("bot", reply);
        history.push({ role: "assistant", content: reply });
        persistHistory();
        if (data.should_book_demo && data.booking_url) {
          renderCta(data.booking_url);
        }
      })
      .catch(function () {
        typingEl.remove();
        renderMessage(
          "bot",
          "Verbindungsfehler. Bitte überprüfe deine Internetverbindung und versuch es erneut."
        );
      })
      .finally(function () {
        sending = false;
        sendBtn.disabled = false;
        attachBtn.disabled = false;
      });
  }

  sendBtn.addEventListener("click", sendMessage);
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter") sendMessage();
  });

  // Kleine öffentliche API für Seiten, die den Chat z. B. per eigenem
  // "Jetzt chatten"-Button statt der Bubble öffnen wollen -- optional,
  // die Ein-Zeilen-Einbindung oben braucht das nicht.
  window.NovaraChatWidget = {
    open: function () {
      setOpen(true);
    },
    close: function () {
      setOpen(false);
    },
    reset: function () {
      resetSession();
    },
  };
})();
