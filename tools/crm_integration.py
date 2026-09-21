"""
CRM / ERP Integration Tool.

CRMIntegration (Operations-Agent, Rechnungen): weiterhin reiner In-Memory-
Mock. TODO: vor Einsatz auf echtes ERP umstellen (_post_to_erp() durch
httpx-Client ersetzen) — Interface bleibt identisch.

CRMIntegrationSDR (SDR-Agent, Leads): Mock per Default, mit zwei möglichen
echten Schreibpfaden zum selben Google Sheet — siehe upsert_lead():
- settings.crm_service_account_configured (tools/production_crm_bridge.py,
  21.09.2026): Service-Account-Auth, funktioniert auf Railway.
- settings.sdr_crm_live_sheet (Block C1, 10.09.2026, tools/live_crm_bridge.py
  → crm_handler.py im Repo la-maquina-de-confianza): OAuth-Token, an diesen
  Mac gebunden, NUR für lokale Entwicklung.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from core.config import settings

logger = logging.getLogger(__name__)


class ERPRecord(BaseModel):
    """Datensatz, der ins ERP/CRM geschrieben wird."""
    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    company_name: str
    amount: float
    currency: str = "EUR"
    invoice_date: str
    invoice_number: Optional[str] = None
    raw_text_hash: Optional[str] = None  # SHA-256 des Quelltexts für Audit-Trail
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    source_agent: str = "operations"


class CRMResult(BaseModel):
    success: bool
    record_id: str
    message: str
    erp_response: dict[str, Any] = Field(default_factory=dict)


class CRMIntegration:
    """
    Mock-CRM-Client. Simuliert POST /records an ein ERP-System.
    Thread-safe: kein gemeinsamer Zustand zwischen Aufrufen.
    """

    def __init__(self, endpoint: str = "https://crm.mock/api/v1", api_key: str = "mock") -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self._mock_store: list[dict] = []  # In-memory store für Dev/Tests

    def upsert_invoice(self, record: ERPRecord) -> CRMResult:
        """
        Schreibt einen Rechnungsdatensatz ins ERP.
        In Produktion: httpx.post(self.endpoint + "/invoices", json=record.model_dump())
        """
        payload = record.model_dump()

        logger.info(
            "CRM upsert_invoice called",
            extra={"record_id": record.record_id, "company": record.company_name},
        )

        # --- Mock: Lokale Speicherung ---
        self._mock_store.append(payload)

        # Simulate ERP response
        mock_erp_response = {
            "erp_id": f"ERP-{record.record_id[:8].upper()}",
            "status": "CREATED",
            "endpoint": self.endpoint,
            "timestamp": datetime.utcnow().isoformat(),
        }

        logger.info("CRM record created", extra={"erp_id": mock_erp_response["erp_id"]})

        return CRMResult(
            success=True,
            record_id=record.record_id,
            message=f"Invoice from '{record.company_name}' successfully stored in ERP.",
            erp_response=mock_erp_response,
        )

    def get_all_mock_records(self) -> list[dict]:
        """Nur für Tests/Dev – gibt alle gespeicherten Mock-Datensätze zurück."""
        return list(self._mock_store)


# ── Lead / SDR records ────────────────────────────────────────────────────────

class LeadRecord(BaseModel):
    """Lead-Datensatz, der vom SDR Agent ins CRM geschrieben wird."""
    lead_id: str = Field(default_factory=lambda: f"LEAD-{uuid.uuid4().hex[:8].upper()}")
    company_name: str
    contact_name: str
    contact_title: str
    contact_email: Optional[str] = None
    contact_linkedin: Optional[str] = None
    industry: str
    company_size: Optional[int] = None
    lead_score: int
    icp_tier: str                       # "high" | "medium" | "low"
    outreach_channel: str               # "email" | "linkedin"
    outreach_subject: Optional[str] = None
    outreach_text: str
    pain_points: list[str] = Field(default_factory=list)
    contact_source: str = "database"    # "database" | "generated"
    source_agent: str = "sdr"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class LeadCRMResult(BaseModel):
    success: bool
    lead_id: str
    message: str
    crm_response: dict[str, Any] = Field(default_factory=dict)


def _lead_record_to_sheet_row(record: LeadRecord) -> dict[str, str]:
    """
    Bildet LeadRecord auf die Spalten von crm_handler.add_lead_to_crm() ab
    (firma, ansprechpartner, position, email, telefon, ...). Telefon wird
    bewusst leer gelassen — das SDR-Agent-Datenmodell (LeadRecord,
    ProspectContact) erfasst aktuell an keiner Stelle eine Telefonnummer;
    das ist eine separate, spätere Erweiterung der Pipeline, keine Lücke in
    dieser Kopplung. "Branche" hat in der Sheet-Struktur keine eigene
    Spalte und landet deshalb zusammen mit Score/Pain-Points in Notizen.
    """
    notizen_parts = [f"Branche: {record.industry}"]
    if record.company_size:
        notizen_parts.append(f"Größe: {record.company_size}")
    notizen_parts.append(f"Score: {record.lead_score} ({record.icp_tier})")
    if record.pain_points:
        notizen_parts.append(f"Pain Points: {', '.join(record.pain_points)}")
    notizen_parts.append(f"Quelle: SDR-Agent ({record.contact_source})")

    return {
        "firma": record.company_name,
        "ansprechpartner": record.contact_name,
        "position": record.contact_title,
        "email": record.contact_email or "",
        "telefon": "",
        "website": "",
        "quelle": "SDR-Agent",
        "notizen": " | ".join(notizen_parts),
    }


class CRMIntegrationSDR(CRMIntegration):
    """Extends CRMIntegration with SDR-specific lead management."""

    def upsert_lead(self, record: LeadRecord) -> LeadCRMResult:
        """
        Schreibt einen Lead-Datensatz ins CRM.

        Standardmäßig In-Memory-Mock (_mock_store). Zwei mögliche echte
        Schreibpfade, geprüft in dieser Reihenfolge:
        1. settings.crm_service_account_configured (Produktion, seit
           21.09.2026, siehe tools/production_crm_bridge.py) — Service-
           Account-Auth, funktioniert auf Railway.
        2. settings.sdr_crm_live_sheet (Block C1, NUR lokale Entwicklung —
           siehe tools/live_crm_bridge.py) — OAuth-Token, an diesen Mac
           gebunden.
        Beide zielen auf dasselbe Google Sheet (siehe production_crm_bridge-
        Docstring) und sind bewusst nie gleichzeitig aktiv erwartet (lokal
        entweder das eine ODER das andere Flag setzen, nie beide). Schlägt
        ein aktiver Live-Schreibpfad fehl, wird das bewusst als
        success=False gemeldet statt hinter dem Mock versteckt.
        """
        payload = record.model_dump()
        self._mock_store.append(payload)

        logger.info(
            "CRM upsert_lead called",
            extra={"lead_id": record.lead_id, "company": record.company_name, "score": record.lead_score},
        )

        if settings.crm_service_account_configured:
            from tools.production_crm_bridge import add_lead_to_crm

            try:
                written_row = add_lead_to_crm(_lead_record_to_sheet_row(record))
            except Exception as exc:
                logger.warning(
                    "Produktions-CRM-Schreibversuch fehlgeschlagen",
                    extra={"lead_id": record.lead_id, "error": str(exc)},
                )
                return LeadCRMResult(
                    success=False,
                    lead_id=record.lead_id,
                    message=f"Produktions-CRM-Schreibversuch fehlgeschlagen: {exc}",
                    crm_response={},
                )

            logger.info(
                "Lead ins Produktions-CRM-Sheet geschrieben",
                extra={"lead_id": record.lead_id, "sheet_row_id": written_row.get("ID")},
            )
            return LeadCRMResult(
                success=True,
                lead_id=record.lead_id,
                message=(
                    f"Lead '{record.contact_name}' @ '{record.company_name}' "
                    f"erfolgreich im Live-Google-Sheet angelegt (Zeile {written_row.get('ID')})."
                ),
                crm_response={"sheet_row": written_row},
            )

        if settings.sdr_crm_live_sheet:
            from tools.live_crm_bridge import add_lead_to_live_crm

            try:
                written_row = add_lead_to_live_crm(_lead_record_to_sheet_row(record))
            except Exception as exc:
                logger.warning(
                    "Live-CRM-Schreibversuch fehlgeschlagen",
                    extra={"lead_id": record.lead_id, "error": str(exc)},
                )
                return LeadCRMResult(
                    success=False,
                    lead_id=record.lead_id,
                    message=f"Live-CRM-Schreibversuch fehlgeschlagen: {exc}",
                    crm_response={},
                )

            logger.info(
                "Lead ins Live-CRM-Sheet geschrieben",
                extra={"lead_id": record.lead_id, "sheet_row_id": written_row.get("ID")},
            )
            return LeadCRMResult(
                success=True,
                lead_id=record.lead_id,
                message=(
                    f"Lead '{record.contact_name}' @ '{record.company_name}' "
                    f"erfolgreich im Live-Google-Sheet angelegt (Zeile {written_row.get('ID')})."
                ),
                crm_response={"sheet_row": written_row},
            )

        crm_response = {
            "crm_id": f"CRM-{record.lead_id}",
            "status": "CREATED",
            "pipeline": "outbound-sdr",
            "stage": "new_lead",
            "endpoint": self.endpoint,
            "timestamp": datetime.utcnow().isoformat(),
        }

        logger.info("CRM lead created", extra={"crm_id": crm_response["crm_id"]})

        return LeadCRMResult(
            success=True,
            lead_id=record.lead_id,
            message=(
                f"Lead '{record.contact_name}' @ '{record.company_name}' "
                f"erfolgreich in CRM-Pipeline angelegt."
            ),
            crm_response=crm_response,
        )
