"""
CRM / ERP Integration Tool – Mock-Implementierung.
In Produktion: Ersetze _post_to_erp() durch echten HTTP-Client (httpx).
Das Interface bleibt identisch – keine Änderungen am aufrufenden Code nötig.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

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


class CRMIntegrationSDR(CRMIntegration):
    """Extends CRMIntegration with SDR-specific lead management."""

    def upsert_lead(self, record: LeadRecord) -> LeadCRMResult:
        """
        Schreibt einen Lead-Datensatz ins CRM.
        In Produktion: httpx.post(self.endpoint + "/leads", json=record.model_dump())
        """
        payload = record.model_dump()
        self._mock_store.append(payload)

        logger.info(
            "CRM upsert_lead called",
            extra={"lead_id": record.lead_id, "company": record.company_name, "score": record.lead_score},
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
