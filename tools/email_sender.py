"""
Echter E-Mail-Versand über dieselbe Gmail-OAuth-Brücke zum Schwester-Repo
la-maquina-de-confianza wie tools/live_crm_bridge.py (scope gmail.send ist dort
bereits erteilt, aber bislang unbenutzt — crm_handler.py hat bisher nur
gelesen, nie gesendet).

NUR FÜR LOKALE ENTWICKLUNG — siehe tools/live_crm_bridge.py für die
Begründung (ein Railway-Deploy von novara-agents hat keinen Zugriff auf das
an diesen Mac gebundene OAuth-Token).

Sendet IMMER als das im Token hinterlegte Konto (aktuell anton@novaraautomation.com),
NIE "als" die Zieladresse eines Kunden. Nur aktiv, wenn der aufrufende Code das
bewusst anfordert — siehe core/config.py, support_escalation_email_live.
"""
from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Any

from tools.live_crm_bridge import _load_crm_handler


def send_email(to: str, subject: str, body: str) -> dict[str, Any]:
    """Sendet eine echte E-Mail über das Gmail-Konto aus la-maquina-de-confianza.

    Wirft bei fehlender Konfiguration/Auth/Verbindung eine Exception — der
    Aufrufer soll das bewusst behandeln (loggen, nicht die ganze
    Agent-Antwort zum Absturz bringen), analog zu
    live_crm_bridge.add_lead_to_live_crm().
    """
    handler = _load_crm_handler()
    gmail = handler.get_gmail_service()

    message = MIMEText(body, _charset="utf-8")
    message["to"] = to
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    sent = gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"success": True, "message_id": sent.get("id"), "to": to}
