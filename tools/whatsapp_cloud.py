"""
WhatsApp Cloud API (Meta) -- einziger WhatsApp-Provider von Novara.

Kapselt alles, was main.py POST/GET /api/v1/webhook/whatsapp und die
Demo-Sandbox von Meta brauchen, damit der Webhook-Handler selbst schlank
bleibt:

- verify_signature(): prüft `X-Hub-Signature-256` (HMAC-SHA256 über den
  ROHEN Request-Body mit dem App Secret) -- die einzige Authentifizierung des
  öffentlichen Endpoints (Meta kann keinen X-API-Key mitschicken).
- parse_incoming(): extrahiert die Nachrichten aus dem Webhook-JSON (Meta
  schickt auch Zustellstatus-Events ohne `messages`, die ignoriert werden).
- download_media(): Sprachnachricht über die Graph API laden (2 Schritte:
  Media-ID -> URL -> Bytes, beide mit Bearer-Token).
- send_text() / upload_media() / send_document(): Antworten. Meta kennt
  KEINE synchrone Antwort im Webhook-Response (anders als TwiML) -- jede
  Antwort ist ein eigener Graph-API-Call. Ein PDF wird zuerst als Media
  hochgeladen und dann per Media-ID gesendet: unabhängig davon, dass Railways
  Dateisystem ephemer ist und ohne öffentlich erreichbare PDF-URL.

Alle Netzwerkfunktionen sind synchron (httpx) -- der Aufrufer nutzt
loop.run_in_executor(), gleiche Regel wie überall sonst in main.py. Die
send_*-Funktionen werfen NIE, sondern geben False/None zurück und loggen
(ein Fehler beim Antworten darf den Verarbeitungspfad nicht abbrechen).

Nummernformat: Meta liefert `from` als reine Ziffern mit Ländervorwahl,
ohne "+" ("4917632320243"). Intern wird überall die E.164-Schreibweise mit
"+" verwendet (`normalize_number()`).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from core.config import normalize_number, settings

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    sender: str  # E.164 mit "+", z. B. "+4917632320243"
    msg_type: str  # "text" | "audio" | "image" | "document" | ...
    text: str = ""
    media_id: str = ""
    mime_type: str = ""
    phone_number_id: str = ""


def _graph_url(path: str) -> str:
    return f"https://graph.facebook.com/{settings.whatsapp_graph_api_version}/{path.lstrip('/')}"


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.whatsapp_access_token.get_secret_value()}"}


def _phone_number_id(override: str = "") -> str:
    return settings.whatsapp_phone_number_id or override


def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Prüft `X-Hub-Signature-256: sha256=<hex>` gegen HMAC-SHA256(App Secret, Body).

    Ohne WHATSAPP_APP_SECRET: in Produktion wird ABGELEHNT (fail-closed --
    sonst könnte jeder beliebige Requests schicken, die echte LLM-Calls und
    PDF-Erzeugung auslösen); lokal (ENVIRONMENT != production) wird mit
    Warn-Log durchgelassen, damit die Entwicklung ohne Meta-Zugang möglich
    bleibt. MIT gesetztem Secret ist eine falsche/fehlende Signatur immer ein
    harter Ablehnungsgrund.
    """
    secret = settings.whatsapp_app_secret.get_secret_value()
    if not secret:
        if settings.is_production:
            logger.error("WhatsApp-Webhook: WHATSAPP_APP_SECRET fehlt in Produktion -- Request abgelehnt")
            return False
        logger.warning("WhatsApp-Webhook: WHATSAPP_APP_SECRET nicht gesetzt -- Signaturprüfung übersprungen (nur lokal)")
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        logger.warning("WhatsApp-Webhook: X-Hub-Signature-256-Header fehlt oder hat falsches Format")
        return False

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len("sha256="):])


def verify_challenge(mode: str, token: str, challenge: str) -> Optional[str]:
    """GET-Verifizierung beim Einrichten des Webhooks im Meta-Dashboard:
    gibt `challenge` zurück, wenn mode == "subscribe" und der Verify-Token
    passt, sonst None."""
    expected = settings.whatsapp_verify_token.get_secret_value()
    if mode == "subscribe" and expected and hmac.compare_digest(token or "", expected):
        return challenge
    return None


def parse_incoming(payload: dict[str, Any]) -> list[IncomingMessage]:
    """Alle Nutzernachrichten aus einem Webhook-Payload (Status-Events ohne
    `messages` liefern eine leere Liste)."""
    result: list[IncomingMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            phone_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            for msg in value.get("messages") or []:
                msg_type = str(msg.get("type") or "")
                media = msg.get(msg_type) if isinstance(msg.get(msg_type), dict) else {}
                result.append(
                    IncomingMessage(
                        message_id=str(msg.get("id") or ""),
                        sender=normalize_number(str(msg.get("from") or "")),
                        msg_type=msg_type,
                        text=str((msg.get("text") or {}).get("body") or "") if msg_type == "text" else "",
                        media_id=str(media.get("id") or ""),
                        mime_type=str(media.get("mime_type") or ""),
                        phone_number_id=phone_id,
                    )
                )
    return result


def download_media(media_id: str) -> tuple[bytes, str]:
    """Lädt eine Media-Datei (z. B. Sprachnachricht). Wirft bei Fehlern --
    der Aufrufer (main.py) fängt das mit der passenden Fehlermeldung an den
    Techniker ab. Gibt (Bytes, MIME-Typ) zurück."""
    meta = httpx.get(_graph_url(media_id), headers=_auth_headers(), timeout=_TIMEOUT)
    meta.raise_for_status()
    info = meta.json()
    resp = httpx.get(info["url"], headers=_auth_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.content, str(info.get("mime_type") or resp.headers.get("content-type", ""))


def _post_message(to: str, body: dict[str, Any], phone_number_id: str = "") -> bool:
    if not settings.whatsapp_configured:
        logger.error("WhatsApp-Versand übersprungen -- WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID fehlen")
        return False
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to.lstrip("+"), **body}
    try:
        resp = httpx.post(
            _graph_url(f"{_phone_number_id(phone_number_id)}/messages"),
            headers=_auth_headers(), json=payload, timeout=_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.error("WhatsApp-Versand fehlgeschlagen: HTTP %s %s", resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as exc:
        logger.error("WhatsApp-Versand fehlgeschlagen: %s", exc)
        return False


def send_text(to: str, text: str, phone_number_id: str = "") -> bool:
    return _post_message(to, {"type": "text", "text": {"body": text[:4096], "preview_url": False}}, phone_number_id)


def upload_media(data: bytes, filename: str, mime_type: str = "application/pdf", phone_number_id: str = "") -> Optional[str]:
    """Lädt eine Datei zu Meta hoch und gibt die Media-ID zurück (oder None)."""
    if not settings.whatsapp_configured:
        logger.error("WhatsApp-Upload übersprungen -- WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID fehlen")
        return None
    try:
        resp = httpx.post(
            _graph_url(f"{_phone_number_id(phone_number_id)}/media"),
            headers=_auth_headers(),
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (filename, data, mime_type)},
            timeout=_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.error("WhatsApp-Media-Upload fehlgeschlagen: HTTP %s %s", resp.status_code, resp.text[:300])
            return None
        return str(resp.json().get("id") or "") or None
    except Exception as exc:
        logger.error("WhatsApp-Media-Upload fehlgeschlagen: %s", exc)
        return None


def send_document(to: str, media_id: str, filename: str, caption: str = "", phone_number_id: str = "") -> bool:
    return _post_message(
        to,
        {"type": "document", "document": {"id": media_id, "filename": filename, "caption": caption[:1024]}},
        phone_number_id,
    )
