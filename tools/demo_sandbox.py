"""
Demo-Sandbox für den Baustellen-Voice-Assistant (main.py POST
/api/v1/webhook/whatsapp) -- lässt Prospects/Vertrieb den WhatsApp-Regiebericht-
Flow risikofrei vorführen, ohne echte Kundendaten im Produktions-CRM-Sheet
(settings.crm_sheet_name) zu hinterlassen.

Eine Nachricht gilt als Demo, wenn ENTWEDER der Absender in
settings.whatsapp_demo_test_numbers_set steht ODER der Nachrichtentext
"[DEMO]" enthält (case-insensitive) -- letzteres erlaubt eine spontane
Demo von JEDER Nummer aus, ohne vorher eine Testnummer konfigurieren zu
müssen. Der Marker wird vor der Weitergabe an FieldWorkerAgent entfernt,
damit er nicht versehentlich als Teil der Tätigkeitsbeschreibung ins PDF
rutscht.

Schreibfehler ins Demo-Sheet werden NIE propagiert (gleiche Fail-Safe-
Philosophie wie main.py whatsapp_webhook()s äußeres try/except): ein
kaputtes Google-Sheets-Setup soll die PDF-Antwort an den Demo-Nutzer nicht
verhindern, nur den Sheet-Log-Schritt überspringen.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from core.config import settings
from core.security import SecurityLayer
from tools import production_crm_bridge

logger = logging.getLogger(__name__)

_DEMO_MARKER_RE = re.compile(re.escape("[DEMO]"), re.IGNORECASE)

_DEMO_HEADERS = [
    "Timestamp", "Sender", "Techniker", "Kunde", "Stunden",
    "Material", "Taetigkeit", "Datum",
]


def is_demo_message(sender: str, body_text: str) -> bool:
    """True, wenn der Absender eine konfigurierte Testnummer ist ODER der
    Nachrichtentext den "[DEMO]"-Marker enthält."""
    if sender and sender in settings.whatsapp_demo_test_numbers_set:
        return True
    return bool(_DEMO_MARKER_RE.search(body_text or ""))


def strip_demo_marker(body_text: str) -> str:
    """Entfernt den "[DEMO]"-Marker aus dem Nachrichtentext, damit er nicht
    als Teil der Tätigkeitsbeschreibung ins PDF/an den Agenten gelangt."""
    return _DEMO_MARKER_RE.sub("", body_text or "").strip()


def _ensure_demo_tab(service: Any, spreadsheet_id: str, tab_name: str) -> None:
    """Legt tab_name als neues Sheet-Tab inkl. Kopfzeile an, falls es noch
    nicht existiert -- Demo-Sandbox soll ohne manuelles Sheet-Setup nutzbar
    sein."""
    meta = production_crm_bridge.execute_with_retry(
        service.spreadsheets().get(spreadsheetId=spreadsheet_id),
        "Spreadsheet-Metadaten lesen (Demo-Tab-Check)",
    )
    existing_titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab_name in existing_titles:
        return

    production_crm_bridge.execute_with_retry(
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
        ),
        "Demo-Tab anlegen",
    )
    production_crm_bridge.execute_with_retry(
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab_name}'!A1",
            valueInputOption="RAW",
            body={"values": [_DEMO_HEADERS]},
        ),
        "Demo-Tab-Kopfzeile schreiben",
    )


def log_demo_lead(data: dict[str, Any], sender: str) -> bool:
    """
    Hängt die Regiebericht-Demo-Daten an settings.demo_sheet_tab_name an
    (eigenes Tab im selben Spreadsheet wie das echte CRM, per Service-Account
    -- siehe tools/production_crm_bridge.py). Legt das Tab bei Bedarf selbst
    an. Gibt False zurück statt zu werfen, wenn kein Service-Account
    konfiguriert ist oder der Schreibvorgang fehlschlägt -- der WhatsApp-
    Antwortpfad (main.py) darf davon nicht abhängen.
    """
    if not settings.crm_service_account_configured:
        logger.warning(
            "Demo-Sandbox: kein GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON konfiguriert -- "
            "Demo-Lead wird nicht geloggt"
        )
        return False

    try:
        service = production_crm_bridge.get_sheets_service()
        spreadsheet_id = settings.crm_spreadsheet_id
        tab_name = settings.demo_sheet_tab_name
        _ensure_demo_tab(service, spreadsheet_id, tab_name)

        arbeit_raw = str(data.get("arbeit") or "").strip()
        arbeit_text = SecurityLayer.check_and_redact(arbeit_raw).redacted_text if arbeit_raw else ""

        row = [
            datetime.now(timezone.utc).isoformat(),
            sender or "-",
            str(data.get("techniker") or "-"),
            str(data.get("kunde") or "-"),
            str(data.get("stunden") if data.get("stunden") not in (None, "") else "-"),
            str(data.get("material") or "-"),
            arbeit_text[:500] or "-",
            str(data.get("datum") or "-"),
        ]
        production_crm_bridge.execute_with_retry(
            service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=f"'{tab_name}'!A:H",
                valueInputOption="USER_ENTERED",
                insertDataOption="OVERWRITE",
                body={"values": [row]},
            ),
            "Demo-Lead anhängen",
        )
        logger.info("Demo-Lead ins Sandbox-Sheet geschrieben", extra={"tab": tab_name})
        return True
    except Exception:
        logger.warning("Demo-Sandbox: Schreiben ins Demo-Sheet fehlgeschlagen", exc_info=True)
        return False
