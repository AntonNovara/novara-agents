"""
Ticket-System – Mock-Implementierung.
In Produktion: Ersetze _post_ticket() durch httpx-Aufruf
(Zendesk, Freshdesk, Jira Service Management, …).
Das Interface bleibt identisch – kein Änderungsbedarf im aufrufenden Code.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TicketPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TicketRecord(BaseModel):
    ticket_id: str = Field(default_factory=lambda: f"TKT-{uuid.uuid4().hex[:8].upper()}")
    subject: str
    description: str
    intent: str
    priority: TicketPriority
    sentiment: str
    language: str
    requester_session: str
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    source_agent: str = "support"


class TicketResult(BaseModel):
    success: bool
    ticket_id: str
    priority: TicketPriority
    message: str
    system_response: dict[str, Any] = Field(default_factory=dict)


class TicketSystem:
    """
    Mock-Ticket-Client. Simuliert POST /tickets an ein Helpdesk-System.
    Thread-safe: kein gemeinsamer Zustand zwischen Aufrufen.
    """

    def __init__(self, endpoint: str = "https://support.mock/api/v1") -> None:
        self.endpoint = endpoint
        self._mock_store: list[dict] = []

    def create_ticket(self, record: TicketRecord) -> TicketResult:
        payload = record.model_dump()
        self._mock_store.append(payload)

        logger.info(
            "Ticket created",
            extra={"ticket_id": record.ticket_id, "priority": record.priority.value},
        )

        return TicketResult(
            success=True,
            ticket_id=record.ticket_id,
            priority=record.priority,
            message=(
                f"Ihr Ticket {record.ticket_id} wurde erfolgreich angelegt "
                f"(Priorität: {record.priority.value.upper()}). "
                "Unser Support-Team meldet sich gemäß SLA."
            ),
            system_response={
                "url": f"{self.endpoint}/tickets/{record.ticket_id}",
                "status": "OPEN",
                "queue": f"support-{record.intent}",
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

    def get_all_mock_tickets(self) -> list[dict]:
        return list(self._mock_store)
