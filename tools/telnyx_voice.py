"""
Telnyx Call Control -- Handler für den "Anfragen-Starter" (Anruf verpasst -> Hinweis).

Ablauf (siehe Trabajo-2026-09-25/D_meta_y_telefonia und E_telnyx): der Handwerker
leitet sein Mobil/Festnetz BEDINGT (nicht erreichbar / besetzt / keine Antwort) auf eine
virtuelle österreichische Telnyx-Nummer um. Für jeden umgeleiteten Anruf schickt Telnyx
Webhook-Events an POST /api/v1/webhook/telnyx/voice (main.py); dieses Modul entscheidet
daraus die Aktionen:

  call.initiated (incoming)  -> Anruf als "verpasst" speichern + ANNEHMEN (answer)
  call.answered              -> Ansage sprechen (speak, de-DE): Betrieb nicht erreichbar,
                                bitte per WhatsApp schreiben (das schafft Opt-in und das
                                kostenlose 24-h-Fenster der WhatsApp Cloud API)
  call.speak.ended           -> AUFLEGEN (hangup)
  call.hangup                -> Status abschließen

Sicherheit: die Signatur jedes Webhooks (Ed25519, Header `telnyx-signature-ed25519` +
`telnyx-timestamp`, signiert wird `timestamp|rohbody`) wird IMMER geprüft -- ohne
TELNYX_PUBLIC_KEY wird alles abgelehnt (fail-closed, auch lokal): dieser Endpoint löst
kostenpflichtige Anruf-Befehle aus. Es wird nichts aufgezeichnet (kein `record`).

STATUS: gegen die Telnyx-Dokumentation gebaut, NOCH NICHT gegen eine echte Telnyx-Nummer
getestet. Bei der ersten echten Testnummer prüfen: Name der deutschen TTS-Stimme
(TELNYX_VOICE), echte Payloads von call.answered/call.speak.ended/call.hangup und ob bei
umgeleiteten Anrufen die Original-Rufnummer in `from` ankommt (hängt vom Betreiber ab).

Alle Netzwerkfunktionen sind synchron (httpx) und werfen NIE (Aufrufer: BackgroundTasks).
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from core.config import normalize_number, settings
from core.db import Base, SessionLocal, engine

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telnyx.com/v2"
_TIMEOUT = 10.0
_MAX_CLOCK_SKEW_SECONDS = 300

_DIGITS_DE = {"0": "null", "1": "eins", "2": "zwei", "3": "drei", "4": "vier",
              "5": "fünf", "6": "sechs", "7": "sieben", "8": "acht", "9": "neun"}


# ── Persistenz ─────────────────────────────────────────────────────────────────

class _MissedCallRow(Base):
    __tablename__ = "missed_calls"

    call_session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    from_number: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    to_number: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="received")


Base.metadata.create_all(bind=engine, tables=[_MissedCallRow.__table__])

_STATUS_ORDER = ["received", "answered", "spoken", "hangup"]


def _mask(number: str) -> str:
    """Rufnummern sind personenbezogen: in Logs nur die letzten 3 Ziffern."""
    return f"***{number[-3:]}" if number and len(number) > 3 else "***"


def record_call(call_session_id: str, from_number: str, to_number: str, occurred_at: Optional[datetime] = None) -> bool:
    """Speichert einen verpassten Anruf (idempotent über call_session_id). True, wenn neu."""
    if not call_session_id:
        return False
    with SessionLocal() as session:
        if session.get(_MissedCallRow, call_session_id) is not None:
            return False
        session.add(_MissedCallRow(
            call_session_id=call_session_id,
            from_number=from_number or "unknown",
            to_number=to_number or "",
            occurred_at=occurred_at or datetime.now(timezone.utc),
            status="received",
        ))
        session.commit()
    return True


def advance_status(call_session_id: str, status: str) -> None:
    """Setzt den Status nur VORWÄRTS (Events können doppelt/vertauscht ankommen)."""
    if not call_session_id or status not in _STATUS_ORDER:
        return
    with SessionLocal() as session:
        row = session.get(_MissedCallRow, call_session_id)
        if row is not None and _STATUS_ORDER.index(status) > _STATUS_ORDER.index(row.status):
            row.status = status
            session.commit()


def list_recent(limit: int = 50) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.query(_MissedCallRow).order_by(_MissedCallRow.occurred_at.desc()).limit(limit).all()
        return [
            {"call_session_id": r.call_session_id, "from": r.from_number, "to": r.to_number,
             "occurred_at": r.occurred_at.isoformat(), "status": r.status}
            for r in rows
        ]


# ── Signatur ───────────────────────────────────────────────────────────────────

def verify_signature(raw_body: bytes, signature_b64: str, timestamp: str, now: Optional[float] = None) -> bool:
    """Ed25519-Prüfung wie in der Telnyx-Doku: Nachricht = timestamp + "|" + roher Body,
    Toleranz 5 Minuten. Ohne konfigurierten öffentlichen Schlüssel: immer False."""
    public_key_b64 = (settings.telnyx_public_key or "").strip()
    if not public_key_b64:
        logger.error("Telnyx-Webhook abgelehnt: TELNYX_PUBLIC_KEY nicht gesetzt")
        return False
    try:
        ts = int(timestamp)
        if abs((time.time() if now is None else now) - ts) > _MAX_CLOCK_SKEW_SECONDS:
            logger.warning("Telnyx-Webhook abgelehnt: Zeitstempel außerhalb der Toleranz")
            return False
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), timestamp.encode("utf-8") + b"|" + raw_body)
        return True
    except (InvalidSignature, ValueError, TypeError):
        logger.warning("Telnyx-Webhook abgelehnt: ungültige Signatur")
        return False


# ── Ansage ─────────────────────────────────────────────────────────────────────

def spoken_number(number: str) -> str:
    """"+4366412345678" -> "plus vier drei, sechs sechs, vier eins, ..." (Ziffern einzeln,
    in Zweiergruppen, damit TTS sie verständlich vorliest)."""
    digits = [c for c in (number or "") if c.isdigit()]
    words = [_DIGITS_DE[d] for d in digits]
    groups = [" ".join(words[i:i + 2]) for i in range(0, len(words), 2)]
    return ("plus " if (number or "").strip().startswith("+") else "") + ", ".join(groups)


def build_announcement() -> str:
    """Deutsche Ansage. Nennt die WhatsApp-Nummer nur, wenn sie konfiguriert ist."""
    name = (settings.missed_call_business_name or "").strip() or "der Betrieb"
    text = f"Guten Tag. {name} ist im Moment leider nicht erreichbar."
    number = normalize_number(settings.missed_call_whatsapp_number)
    if number:
        spoken = spoken_number(number)
        text += (f" Bitte schreiben Sie uns kurz per WhatsApp an die Nummer {spoken}. "
                 f"Ich wiederhole: {spoken}. Wir melden uns so schnell wie möglich.")
    else:
        text += " Bitte versuchen Sie es später erneut."
    return text + " Dies ist eine automatische Ansage. Auf Wiederhören."


# ── Event -> Aktion ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Action:
    name: str  # "answer" | "speak" | "hangup"
    call_control_id: str
    body: dict[str, Any]


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def process_event(event: dict[str, Any]) -> Optional[Action]:
    """Verarbeitet EIN Telnyx-Event (der Inhalt von `data`): Zustand speichern und die
    nächste Aktion zurückgeben (oder None). Rein deterministisch, ohne Netzwerk."""
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload") or {}
    call_control_id = str(payload.get("call_control_id") or "")
    session_id = str(payload.get("call_session_id") or "")

    if event_type == "call.initiated":
        if str(payload.get("direction") or "") != "incoming" or not call_control_id:
            return None
        from_number = str(payload.get("from") or "")
        new = record_call(session_id, from_number, str(payload.get("to") or ""), _parse_time(event.get("occurred_at")))
        logger.info("Verpasster Anruf %s (von %s)", "gespeichert" if new else "bereits bekannt", _mask(from_number))
        return Action("answer", call_control_id, {"command_id": f"answer-{call_control_id}"})

    if not call_control_id:
        return None

    if event_type == "call.answered":
        advance_status(session_id, "answered")
        return Action("speak", call_control_id, {
            "payload": build_announcement(),
            "voice": settings.telnyx_voice,
            "language": "de-DE",
            "client_state": base64.b64encode(b"announcement").decode(),
            "command_id": f"speak-{call_control_id}",
        })

    if event_type == "call.speak.ended":
        advance_status(session_id, "spoken")
        return Action("hangup", call_control_id, {"command_id": f"hangup-{call_control_id}"})

    if event_type == "call.hangup":
        advance_status(session_id, "hangup")
    return None


# ── Befehle senden ─────────────────────────────────────────────────────────────

def execute(action: Action) -> bool:
    """Schickt den Befehl an die Telnyx-API. Wirft nie; False bei Fehlern/ohne API-Key."""
    api_key = settings.telnyx_api_key.get_secret_value()
    if not api_key:
        logger.error("Telnyx-Befehl %s übersprungen: TELNYX_API_KEY nicht gesetzt", action.name)
        return False
    try:
        resp = httpx.post(
            f"{_API_BASE}/calls/{action.call_control_id}/actions/{action.name}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=action.body, timeout=_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.error("Telnyx-Befehl %s fehlgeschlagen: HTTP %s %s", action.name, resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as exc:
        logger.error("Telnyx-Befehl %s fehlgeschlagen: %s", action.name, exc)
        return False
