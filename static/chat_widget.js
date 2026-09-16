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
 *   data-visitor-name / data-visitor-email / data-visitor-company
 *                      Optional, wenn die Seite die Besucheridentität schon
 *                      kennt (z. B. eingeloggter Bereich) -- wird 1:1 als
 *                      visitor_info an den Endpoint durchgereicht.
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
    "box-shadow:0 4px 16px rgba(0,0,0,.3);font-size:26px;z-index:2147483000;" +
    "display:flex;align-items:center;justify-content:center;padding:0;transition:transform .15s ease;}" +
    "#novara-chat-bubble:hover{transform:scale(1.06);}" +
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
    "#novara-chat-send{background:" + CONFIG.accentColor + ";color:#fff;border:none;border-radius:50%;" +
    "width:38px;height:38px;cursor:pointer;font-size:16px;flex-shrink:0;}" +
    "#novara-chat-send:disabled{opacity:.5;cursor:default;}" +
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
  bubble.textContent = "💬";

  var panel = document.createElement("div");
  panel.id = "novara-chat-panel";
  panel.innerHTML =
    '<div id="novara-chat-header"><span>' + escapeHtml(CONFIG.title) + "</span>" +
    '<button id="novara-chat-close" type="button" aria-label="Schließen">×</button></div>' +
    '<div id="novara-chat-messages"></div>' +
    '<div id="novara-chat-input-row">' +
    '<input id="novara-chat-input" type="text" placeholder="Nachricht schreiben..." autocomplete="off" />' +
    '<button id="novara-chat-send" type="button" aria-label="Senden">➤</button>' +
    "</div>";

  document.body.appendChild(bubble);
  document.body.appendChild(panel);

  var messagesEl = panel.querySelector("#novara-chat-messages");
  var inputEl = panel.querySelector("#novara-chat-input");
  var sendBtn = panel.querySelector("#novara-chat-send");
  var closeBtn = panel.querySelector("#novara-chat-close");

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

  // ── Networking ───────────────────────────────────────────────────────────

  var sending = false;

  function sendMessage() {
    var text = inputEl.value.trim();
    if (!text || sending) return;

    renderMessage("user", text);
    history.push({ role: "user", content: text });
    persistHistory();
    inputEl.value = "";

    sending = true;
    sendBtn.disabled = true;
    var typingEl = renderMessage("bot", "…");
    typingEl.classList.add("novara-msg-typing");

    fetch(CONFIG.apiBase + CONFIG.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        message: text,
        visitor_info: {
          name: CONFIG.visitorName,
          email: CONFIG.visitorEmail,
          company: CONFIG.visitorCompany,
          phone: "",
        },
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
  };
})();
