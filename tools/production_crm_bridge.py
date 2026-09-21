"""
Produktions-Brücke zum Live-CRM (Google Sheets) — Gegenstück zu
tools/live_crm_bridge.py, das NUR lokal funktioniert (OAuth-Token, an diesen
Mac gebunden, siehe dessen Docstring). Diese Datei nutzt stattdessen einen
Google-Service-Account (settings.google_sheets_service_account_json) — kein
interaktiver Browser-Login nötig, funktioniert also auf Railway.

Zielt bewusst auf DASSELBE Sheet/Tab wie la-maquina-de-confianza/
crm_handler.py (settings.crm_spreadsheet_id/crm_sheet_name, Default =
dieselben Werte wie dort in .env) — dieselbe Spaltenstruktur (HEADERS unten,
Kopie von crm_handler.HEADERS) und dasselbe sequentielle L-XXXX-ID-Schema,
damit beide Schreibpfade (lokaler Dev-Lauf über crm_handler.py, Railway über
diese Datei) denselben Datensatz-Kontrakt einhalten und sich nicht
gegenseitig ins Gehege kommen.

EIN bewusster Unterschied zu crm_handler.py: die DLP-Sanitisierung läuft
hier über core.security.SecurityLayer (Novaras eigene, bereits an jeder
anderen Stelle in diesem Repo genutzte und getestete DLP-Schicht) statt über
utils/sanitizer.py aus dem Schwester-Repo -- letzteres ist für ein
Railway-Deploy von novara-agents ohnehin nicht erreichbar (siehe
tools/live_crm_bridge.py), und ein zweites, unabhängiges DLP-Regelwerk für
denselben Zweck zu pflegen wäre reine Duplikation ohne Mehrwert.

Nur add_lead_to_crm() ist hier nachgebaut -- initialize_crm_sheet(),
initialize_dashboard_tab(), migrate_old_leads() und check_gmail_replies()
sind einmalige Setup-/Wartungsoperationen, die weiterhin ausschließlich
lokal über crm_handler.py laufen; ein Railway-Deploy schreibt nur laufend
neue Leads an, richtet das Sheet nicht neu ein.
"""
from __future__ import annotations

import json
import logging
import socket
import time
from datetime import date
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from core.config import settings
from core.security import SecurityLayer

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Identisch zu la-maquina-de-confianza/crm_handler.py HEADERS -- siehe
# Moduldocstring für die Begründung, warum beide Schreibpfade dasselbe
# Spaltenschema teilen müssen.
_HEADERS = [
    "ID", "Firma", "Ansprechpartner", "Position", "Email", "Telefon",
    "Website", "Status", "Quelle", "Letzter_Kontakt", "Naechster_Schritt",
    "Angebot_EUR", "Notizen", "Thread_ID", "DLP-Status",
]
_LEAD_FIELDS = [
    "firma", "ansprechpartner", "position", "email", "telefon",
    "website", "status", "quelle", "letzterkontakt",
    "naechsterschritt", "angeboteur", "notizen", "threadid",
]
# Strukturierte/kontrollierte Felder, die die DLP-Schicht nicht redigieren
# soll -- gleiche Begründung wie crm_handler.py's _DLP_EXEMPT_FIELDS.
_DLP_EXEMPT_FIELDS = {
    "email", "telefon", "website", "status", "quelle",
    "letzterkontakt", "naechsterschritt", "angeboteur", "threadid",
}

_LAST_COL_LETTER = chr(ord("A") + len(_HEADERS) - 1)
_MAX_RETRIES = 5
_RETRYABLE_STATUS = {429, 500, 502, 503}

_service: Any = None  # lazy-gebauter, gecachter Sheets-API-Client


class ProductionCRMError(Exception):
    """Basis-Fehler für diese Brücke -- CRMIntegrationSDR.upsert_lead() fängt sie ab."""


def _get_service() -> Any:
    global _service
    if _service is not None:
        return _service

    raw = settings.google_sheets_service_account_json.get_secret_value().strip()
    if not raw:
        raise ProductionCRMError(
            "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON ist nicht gesetzt."
        )
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProductionCRMError(
            f"GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON ist kein valides JSON: {exc}"
        ) from exc

    try:
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=_SCOPES
        )
        _service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise ProductionCRMError(f"Service-Account-Authentifizierung fehlgeschlagen: {exc}") from exc
    return _service


def _execute(request: Any, description: str) -> dict:
    """Request mit Exponential-Backoff bei Quota-/Server-Fehlern -- gleiche Logik wie crm_handler.py._execute()."""
    delay = 1.0
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return request.execute()
        except HttpError as exc:
            status = exc.resp.status if exc.resp is not None else None
            if status in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                logger.warning(
                    "Sheets-API Retry",
                    extra={"description": description, "status": status, "attempt": attempt},
                )
                time.sleep(delay)
                delay *= 2
                continue
            if status in (401, 403):
                raise ProductionCRMError(
                    f"{description}: Zugriff verweigert (HTTP {status}). Hat der "
                    f"Service-Account Bearbeiter-Zugriff auf das Sheet?"
                ) from exc
            if status == 404:
                raise ProductionCRMError(
                    f"{description}: Ressource nicht gefunden (HTTP 404). "
                    f"CRM_SPREADSHEET_ID/CRM_SHEET_NAME prüfen."
                ) from exc
            raise ProductionCRMError(f"{description}: HTTP {status} — {exc}") from exc
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "Sheets-API Netzwerkfehler, Retry",
                    extra={"description": description, "attempt": attempt},
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise ProductionCRMError(
                f"{description}: Verbindung nach {_MAX_RETRIES} Versuchen fehlgeschlagen — {exc}"
            ) from exc
    raise ProductionCRMError(f"{description}: unerwarteter Retry-Abbruch")  # unreachable


def _as_cell_text(value: str) -> str:
    """
    Führendes Apostroph gegen Formel-Interpretation (=, +, @) -- z. B.
    Telefonnummern wie '+43...'. `value and value[0] in "=+@"`, NICHT
    `value[:1] in "=+@"` (letzteres ist der exakte Wortlaut aus
    crm_handler.py._as_cell_text() -- ein echter, dort noch offener Bug: für
    value="" ist value[:1] == "", und "" in "=+@" ist in Python True [leerer
    String ist Teilstring jedes Strings], nicht False. JEDE leere Zelle
    bekäme dadurch ein einzelnes Apostroph statt leer zu bleiben --
    verifiziert per echtem Testschreibvorgang gegen das Produktions-Sheet
    (Zeile L-0093, sofort wieder gelöscht) am 21.09.2026.
    """
    return f"'{value}" if value and value[0] in "=+@" else value


def _apply_dlp(normalized: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Sanitisiert alle Lead-Felder über core.security.SecurityLayer (Kontaktfelder ausgenommen)."""
    findings: list[str] = []
    values: dict[str, str] = {}
    for field_name in _LEAD_FIELDS:
        raw = normalized.get(field_name)
        text = "" if raw is None else str(raw).strip()
        if text and field_name not in _DLP_EXEMPT_FIELDS:
            result = SecurityLayer.check_and_redact(text)
            values[field_name] = result.redacted_text
            findings.extend(result.findings)
        else:
            values[field_name] = text
    dlp_status = f"Masked by DLP ({'; '.join(findings)})" if findings else "Clean"
    return values, dlp_status


def _build_row(lead_id: str, values: dict[str, str], dlp_status: str) -> list[str]:
    return [lead_id, *(_as_cell_text(values[f]) for f in _LEAD_FIELDS), dlp_status]


def add_lead_to_crm(lead_data: dict[str, Any]) -> dict[str, Any]:
    """
    Sanitisiert einen Lead und hängt ihn ans CRM-Sheet an -- Produktions-
    Äquivalent zu crm_handler.add_lead_to_crm() (siehe dessen Docstring für
    das vollständige lead_data-Schema). Wirft ProductionCRMError bei
    fehlender Konfiguration/Auth/Verbindung; CRMIntegrationSDR.upsert_lead()
    meldet das als success=False, statt es hinter dem Mock zu verstecken
    (gleiche Philosophie wie tools/live_crm_bridge.py).
    """
    if not isinstance(lead_data, dict):
        raise ProductionCRMError("lead_data muss ein dict sein.")

    normalized = {str(k).strip().lower().replace("-", ""): v for k, v in lead_data.items()}
    normalized.setdefault("status", "Neu")
    normalized.setdefault("letzterkontakt", date.today().strftime("%d.%m.%Y"))

    values, dlp_status = _apply_dlp(normalized)

    service = _get_service()
    spreadsheet_id = settings.crm_spreadsheet_id
    sheet_name = settings.crm_sheet_name

    id_column = _execute(
        service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"'{sheet_name}'!A:A"
        ),
        "Zeilenanzahl ermitteln",
    ).get("values", [])
    if not id_column:
        raise ProductionCRMError(
            f"Sheet-Tab '{sheet_name}' hat keine Kopfzeile -- wurde es bereits über "
            f"crm_handler.py --setup initialisiert?"
        )

    row = _build_row(f"L-{len(id_column):04d}", values, dlp_status)

    _execute(
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A:{_LAST_COL_LETTER}",
            valueInputOption="USER_ENTERED",
            # OVERWRITE, nicht INSERT_ROWS -- gleiche Begründung wie
            # crm_handler.py: ein echtes Einfügen würde bereichsgebundene
            # Objekte (Dropdowns, Conditional Formatting) unter der Zeile
            # verschieben, die sie eigentlich abdecken sollen.
            insertDataOption="OVERWRITE",
            body={"values": [row]},
        ),
        "Lead anhängen",
    )

    written = dict(zip(_HEADERS, row))
    logger.info("Lead ins Produktions-CRM-Sheet geschrieben", extra={"sheet_row_id": written["ID"]})
    return written
