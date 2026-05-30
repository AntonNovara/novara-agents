"""
Deal Tracker – Mock-Implementierung für den Sales Copilot Agent.
In Produktion: httpx-Client gegen HubSpot Deals API / Salesforce Opportunity.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class DealStage(str, Enum):
    DISCOVERY = "discovery"
    DEMO = "demo"
    PROPOSAL = "proposal"
    NEGOTIATION = "negotiation"
    CLOSING = "closing"


class DealRecord(BaseModel):
    deal_id: str = Field(default_factory=lambda: f"DEAL-{uuid.uuid4().hex[:8].upper()}")
    company_name: str
    contact_name: str
    contact_title: str
    deal_stage: DealStage
    deal_health_score: int       # 0-100
    close_probability: int       # 0-100
    objections: list[dict]       # [{text, category, severity}]
    buying_signals: list[str]
    next_steps: list[str]
    meeting_summary: str
    followup_subject: str
    followup_body: str
    source_agent: str = "sales_copilot"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class DealResult(BaseModel):
    success: bool
    deal_id: str
    message: str
    crm_response: dict[str, Any] = Field(default_factory=dict)


class DealTracker:
    """
    Mock-Deal-Client.
    In Produktion: httpx.post(endpoint + "/deals", json=record.model_dump())
    """

    def __init__(self, endpoint: str = "https://crm.mock/api/v1") -> None:
        self.endpoint = endpoint
        self._mock_store: list[dict] = []

    def upsert_deal(self, record: DealRecord) -> DealResult:
        self._mock_store.append(record.model_dump())

        logger.info(
            "Deal upserted",
            extra={
                "deal_id": record.deal_id,
                "stage": record.deal_stage.value,
                "health": record.deal_health_score,
            },
        )

        return DealResult(
            success=True,
            deal_id=record.deal_id,
            message=(
                f"Deal '{record.deal_id}' für '{record.company_name}' "
                f"(Stage: {record.deal_stage.value}, Health: {record.deal_health_score}/100) gespeichert."
            ),
            crm_response={
                "crm_id": f"CRM-{record.deal_id}",
                "stage": record.deal_stage.value,
                "health_score": record.deal_health_score,
                "close_probability": record.close_probability,
                "endpoint": self.endpoint,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

    def get_all_mock_deals(self) -> list[dict]:
        return list(self._mock_store)
