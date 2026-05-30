"""
BaseAgent – Basisklasse für alle Novara-Agenten.

Erzwingt:
  - Strukturiertes JSON-Logging (Audit-Trail, DSGVO Art. 30)
  - DLP/PII-Prüfung vor jeder Ausgabe
  - Einheitliche Request/Response-Modelle
  - Abstrakte process()-Methode (jeder Agent muss sie implementieren)
"""
import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel, Field

from core.security import SecurityLayer, DLPResult

logger = logging.getLogger(__name__)


class AgentRequest(BaseModel):
    """Eingehende Anfrage an einen Agenten."""
    text: str = Field(..., min_length=1, max_length=32_000, description="Eingabetext für den Agenten")
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResponse(BaseModel):
    """Standardisierte Antwort eines Agenten."""
    success: bool
    session_id: str
    agent_type: str
    result: dict[str, Any] = Field(default_factory=dict)
    dlp_findings: list[str] = Field(default_factory=list)
    processing_time_ms: float = 0.0
    error: Optional[str] = None


class BaseAgent(ABC):
    """
    Abstrakte Basisklasse. Alle Agenten erben hiervon.
    Subklassen implementieren _run() – die public-facing process()-Methode
    übernimmt Security-Checks, Logging und Fehlerbehandlung automatisch.
    """

    agent_type: str = "base"

    def __init__(self) -> None:
        self._logger = logging.getLogger(f"novara.agent.{self.agent_type}")

    def process(self, request: AgentRequest) -> AgentResponse:
        """
        Öffentlicher Einstiegspunkt. Führt vor und nach _run() automatisch aus:
          1. Input-Logging (ohne sensible Daten)
          2. DLP-Check auf dem Eingabetext
          3. Aufruf der Agenten-Logik (_run)
          4. DLP-Check auf der Ausgabe
          5. Audit-Logging mit Laufzeit
        """
        start = time.perf_counter()

        self._logger.info(
            "Agent request received",
            extra={"session_id": request.session_id, "agent": self.agent_type},
        )

        # --- Input DLP ---
        input_dlp: DLPResult = SecurityLayer.check_and_redact(request.text)
        if not input_dlp.approved:
            return AgentResponse(
                success=False,
                session_id=request.session_id,
                agent_type=self.agent_type,
                error=f"Input blocked by DLP: {input_dlp.blocked_reason}",
            )

        # Feed the redacted text into the agent so PII never reaches the LLM
        safe_request = request.model_copy(update={"text": input_dlp.redacted_text})

        try:
            raw_result = self._run(safe_request)
        except Exception as exc:
            self._logger.exception("Agent execution failed", extra={"session_id": request.session_id})
            return AgentResponse(
                success=False,
                session_id=request.session_id,
                agent_type=self.agent_type,
                error=str(exc),
                processing_time_ms=_elapsed_ms(start),
            )

        # --- Output DLP ---
        sanitized_result = SecurityLayer.sanitize_dict(raw_result)

        elapsed = _elapsed_ms(start)
        self._logger.info(
            "Agent request completed",
            extra={
                "session_id": request.session_id,
                "agent": self.agent_type,
                "processing_time_ms": elapsed,
            },
        )

        return AgentResponse(
            success=True,
            session_id=request.session_id,
            agent_type=self.agent_type,
            result=sanitized_result,
            dlp_findings=input_dlp.findings,
            processing_time_ms=elapsed,
        )

    @abstractmethod
    def _run(self, request: AgentRequest) -> dict[str, Any]:
        """
        Agenten-Kernlogik. Muss von jeder Subklasse implementiert werden.
        Gibt ein dict zurück, das in AgentResponse.result landet.
        Darf Exceptions werfen – process() fängt sie ab.
        """
        ...


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)
