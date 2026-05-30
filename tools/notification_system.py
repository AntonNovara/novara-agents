"""
Notification System – Mock-Implementierung für E-Mail-Versand.
In Produktion: httpx-Client gegen SendGrid / Postmark / AWS SES.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SentEmail(BaseModel):
    notification_id: str = Field(default_factory=lambda: f"MAIL-{uuid.uuid4().hex[:8].upper()}")
    to_email: str
    to_name: str
    subject: str
    body: str
    source_agent: str
    sent_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    status: str = "delivered"


class NotificationResult(BaseModel):
    success: bool
    notification_id: str
    message: str
    response: dict[str, Any] = Field(default_factory=dict)


class NotificationSystem:
    """
    Mock-E-Mail-Client.
    In Produktion: httpx.post(provider_url, json={to, subject, html_body, …})
    """

    def __init__(self, provider: str = "mock") -> None:
        self.provider = provider
        self._sent: list[dict] = []

    def send_email(
        self,
        to_email: str,
        to_name: str,
        subject: str,
        body: str,
        source_agent: str = "unknown",
    ) -> NotificationResult:
        email = SentEmail(
            to_email=to_email,
            to_name=to_name,
            subject=subject,
            body=body,
            source_agent=source_agent,
        )
        self._sent.append(email.model_dump())

        logger.info(
            "Email sent (mock)",
            extra={"id": email.notification_id, "to": to_email, "subject": subject},
        )

        return NotificationResult(
            success=True,
            notification_id=email.notification_id,
            message=f"E-Mail '{subject}' an '{to_name}' <{to_email}> zugestellt (mock).",
            response={
                "provider": self.provider,
                "message_id": email.notification_id,
                "status": "delivered",
                "timestamp": email.sent_at,
            },
        )

    def get_all_sent(self) -> list[dict]:
        return list(self._sent)
