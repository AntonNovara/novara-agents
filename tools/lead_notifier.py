"""
E-Mail-Benachrichtigung bei neu erfasstem Lead (core/lead_capture.py).

Anders als tools/email_sender.py (Gmail-OAuth-Brücke zum Schwester-Repo
la-maquina-de-confianza, NUR für lokale Entwicklung — siehe dessen
Docstring) nutzt dieses Modul stinknormales SMTP mit Anwendungspasswort und
funktioniert daher auch auf Railway, wo kein an einen bestimmten Mac
gebundenes OAuth-Token verfügbar ist. Zielserver: Gmail (smtp.gmail.com:587,
STARTTLS). Credentials ausschließlich über die Umgebungsvariablen
SMTP_EMAIL / SMTP_PASSWORD (core/config.py) — niemals hart codiert.

WICHTIG — nie im Request-/Antwortpfad blockieren: `send_lead_notification()`
ist eine normale synchrone Funktion (leicht unit-testbar), aber ihre
Aufrufer sitzen beide auf einem Pfad, der dem Endkunden sofort antworten
muss — main.py landing_chat() (Chat-Antwort an den Website-Besucher) und
main.py voice_webhook()s end-of-call-report-Handler (Response an Vapi).
`notify_lead_async()` unten ist deshalb der einzige Aufrufweg, den beide
Stellen tatsächlich nutzen: sie stößt den SMTP-Versand in einem
Hintergrund-Thread an und kehrt sofort zurück, ohne auf den Netzwerk-
Roundtrip (DNS/Connect/TLS/Login/Send, siehe SMTP-Timeout unten) zu warten.
Ein SMTP-Ausfall (Netzwerk ODER falsche Credentials) landet ausschließlich
als Warn-Log — er darf niemals die Konversation unterbrechen oder einen
HTTP 400/500 an den Client bzw. an Vapi auslösen.
"""
from __future__ import annotations

import logging
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from core.config import settings

if TYPE_CHECKING:
    from core.lead_capture import CapturedLead

logger = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587
# Kurzes Timeout (DNS+Connect+TLS+Login+Send zusammen) -- ein hängender/
# langsamer SMTP-Server soll den Hintergrund-Thread nicht unbegrenzt am
# Leben halten. Wirkt nicht auf die Chat-/Webhook-Latenz (siehe
# notify_lead_async() unten), begrenzt aber, wie lange ein einzelner
# Benachrichtigungsversuch im Hintergrund offen bleibt.
_SMTP_TIMEOUT_SECONDS = 8
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
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=_SMTP_TIMEOUT_SECONDS) as server:
            server.starttls()
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, [_NOTIFY_RECIPIENT], msg.as_string())
        logger.info(
            "Lead-Benachrichtigung verschickt",
            extra={"session": lead.session_id, "source": lead.source},
        )
        return True
    except Exception as exc:
        # Fängt ALLES ab: DNS-Fehler, Verbindungs-Timeout (smtplib respektiert
        # den obigen timeout=_SMTP_TIMEOUT_SECONDS auf jeder Socket-Operation),
        # SMTPAuthenticationError bei falschen Credentials, etc. -- der
        # Aufrufer bekommt so oder so nur True/False, nie eine Exception.
        logger.warning(
            "Lead-Benachrichtigung fehlgeschlagen (Netzwerk oder Credentials): %s",
            exc,
            extra={"session": lead.session_id, "source": lead.source},
        )
        return False


def notify_lead_async(lead: "CapturedLead") -> None:
    """
    Stößt send_lead_notification() in einem Daemon-Hintergrund-Thread an und
    kehrt SOFORT zurück -- das ist der einzige Aufrufweg, den
    agents/sdr_agent.py (InboundChatGraph.finalize(), synchron aus main.py
    landing_chat() heraus aufgerufen) und main.py (voice_webhook()s
    end-of-call-report-Handler) tatsächlich benutzen. Beide Aufrufer sitzen
    auf einem Pfad, der dem Client (Website-Besucher bzw. Vapi) sofort
    antworten muss; ein SMTP-Roundtrip (bis zu _SMTP_TIMEOUT_SECONDS Sekunden
    pro Verbindungsschritt) darf diese Antwort nie verzögern, und ein
    SMTP-Fehler darf sie erst recht nie zu einem HTTP 400/500 machen.

    Markiert den Lead bei Erfolg selbst als benachrichtigt
    (core.lead_capture.mark_notified()) -- der Aufrufer bekommt wegen der
    Hintergrundausführung keinen synchronen Rückgabewert mehr, auf den er
    das stützen könnte. Der try/except um den gesamten Thread-Body ist eine
    zusätzliche Absicherung on top von send_lead_notification()s eigenem
    try/except (das selbst nie wirft) -- schützt zusätzlich gegen einen
    Fehler in mark_notified()/dem Import selbst, damit ein Hintergrund-Thread
    niemals mit einer unbehandelten Exception endet.
    """
    def _run() -> None:
        try:
            if send_lead_notification(lead):
                from core import lead_capture  # lokaler Import: core/lead_capture.py importiert dieses Modul nicht, kein Zyklus

                lead_capture.mark_notified(lead.source, lead.session_id)
        except Exception as exc:
            logger.warning(
                "Lead-Benachrichtigung (Hintergrund-Thread) fehlgeschlagen: %s",
                exc,
                extra={"session": lead.session_id, "source": lead.source},
            )

    threading.Thread(target=_run, name=f"lead-notify-{lead.session_id}", daemon=True).start()


def send_followup_digest(due: list[dict]) -> bool:
    """
    Schickt Anton die Liste der fälligen Follow-ups (tools/sequence_scheduler.
    list_due()) per SMTP. Leere Liste -> keine Mail (True). Wirft NIE.
    """
    if not due:
        return True
    smtp_email = settings.smtp_email.get_secret_value().strip()
    smtp_password = settings.smtp_password.get_secret_value().strip()
    if not smtp_email or not smtp_password:
        logger.warning("Follow-up-Digest übersprungen: SMTP_EMAIL/SMTP_PASSWORD nicht konfiguriert")
        return False

    lines = [f"Heute stehen {len(due)} Follow-up(s) an -- bitte prüfen und selbst versenden:", ""]
    for d in due:
        lines.append(
            f"- {d['lead_key']}: Kanal {d['channel']} (Tag {d['day_offset']}), "
            f"Kontakt: {d.get('identifier') or 'unbekannt'}, fällig seit {d['due_since'][:10]}"
        )
    lines += ["", "Novara verschickt Follow-ups nicht automatisch. Nach dem Versand den Schritt in der Sequenz als erledigt markieren."]

    msg = MIMEText("\n".join(lines))
    msg["From"] = smtp_email
    msg["To"] = _NOTIFY_RECIPIENT
    msg["Subject"] = f"Novara: {len(due)} Follow-up(s) fällig"
    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=_SMTP_TIMEOUT_SECONDS) as server:
            server.starttls()
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, [_NOTIFY_RECIPIENT], msg.as_string())
        return True
    except Exception:
        logger.warning("Follow-up-Digest: SMTP-Versand fehlgeschlagen", exc_info=True)
        return False
