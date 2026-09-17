"""
E-Mail-Benachrichtigung bei neu erfasstem Lead (core/lead_capture.py).

Anders als tools/email_sender.py (Gmail-OAuth-Brücke zum Schwester-Repo
la-maquina-de-confianza, NUR für lokale Entwicklung — siehe dessen
Docstring) nutzt dieses Modul stinknormales SMTP mit Anwendungspasswort und
funktioniert daher auch auf Railway, wo kein an einen bestimmten Mac
gebundenes OAuth-Token verfügbar ist. Zielserver: Gmail (smtp.gmail.com:587,
STARTTLS). Credentials ausschließlich über die Umgebungsvariablen
SMTP_EMAIL / SMTP_PASSWORD (core/config.py) — niemals hart codiert.
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from core.config import settings

if TYPE_CHECKING:
    from core.lead_capture import CapturedLead

logger = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587
_NOTIFY_RECIPIENT = "anton@novaraautomation.com"
_SUBJECT = "\U0001f6a8 Nuevo Lead capturado por IA - Novara Automation"

_SOURCE_LABELS = {"landing_chat": "Website-Chat-Widget", "voice": "Telefon (Vapi)"}


def _build_body(lead: "CapturedLead") -> str:
    source_label = _SOURCE_LABELS.get(lead.source, lead.source)
    return (
        "Ein neuer Lead wurde automatisch von der KI erfasst.\n\n"
        f"Quelle: {source_label}\n"
        f"Session-ID: {lead.session_id}\n"
        f"Erfasst am: {lead.captured_at}\n\n"
        "--- Kontaktdaten ---\n"
        f"Name: {lead.name or '(nicht angegeben)'}\n"
        f"E-Mail: {lead.email or '(nicht angegeben)'}\n"
        f"Telefon: {lead.phone or '(nicht angegeben)'}\n"
        f"Firma: {lead.company or '(nicht angegeben)'}\n\n"
        "--- Zusammenfassung / Auszug der Konversation ---\n"
        f"{lead.message_excerpt or '(kein Auszug verfügbar)'}\n"
    )


def send_lead_notification(lead: "CapturedLead") -> bool:
    """
    Baut und verschickt die Lead-Benachrichtigungsmail per SMTP an
    anton@novaraautomation.com.

    Gibt True bei Erfolg zurück, False bei JEDEM Fehler (fehlende
    Credentials, SMTP-Verbindungs-/Auth-Fehler, ...) — wirft NIE. Gleiche
    Philosophie wie überall in diesem Repo (core/security.py,
    agents/*.py-Fallbacks): ein Benachrichtigungs-Seiteneffekt darf den
    eigentlichen Antwortpfad (Chat-Antwort an den Website-Besucher,
    Voice-Webhook-Response an Vapi) niemals zum Absturz bringen.
    """
    smtp_email = settings.smtp_email.get_secret_value().strip()
    smtp_password = settings.smtp_password.get_secret_value().strip()
    if not smtp_email or not smtp_password:
        logger.warning(
            "Lead-Benachrichtigung übersprungen: SMTP_EMAIL/SMTP_PASSWORD nicht konfiguriert",
            extra={"session": lead.session_id},
        )
        return False

    msg = MIMEMultipart()
    msg["From"] = smtp_email
    msg["To"] = _NOTIFY_RECIPIENT
    msg["Subject"] = _SUBJECT
    msg.attach(MIMEText(_build_body(lead), "plain", "utf-8"))

    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, [_NOTIFY_RECIPIENT], msg.as_string())
        logger.info(
            "Lead-Benachrichtigung verschickt",
            extra={"session": lead.session_id, "source": lead.source},
        )
        return True
    except Exception as exc:
        logger.warning(
            "Lead-Benachrichtigung fehlgeschlagen: %s", exc, extra={"session": lead.session_id}
        )
        return False
