"""
Customer State – geteilter Kundenzustand über die gesamte Journey
(SDR -> Sales Copilot -> Onboarding -> Support -> Operations).

Jeder der 5 Agenten schrieb bisher nur in sein eigenes Tool (LeadRecord in
tools/crm_integration.py, DealRecord in tools/deal_tracker.py, ...) — ein
später aufgerufener Agent hatte keine Möglichkeit, zu sehen, was ein
vorheriger Agent über denselben Kunden bereits herausgefunden hat. Dieses
Modul ist der zentrale, agenten-übergreifende Kundenzustand: jeder Agent
schreibt am Ende seines Workflows einen Snapshot seiner Stufe hierher, und
kann vor seiner eigenen Logik den bisherigen Verlauf lesen (z. B. Support
kennt den Onboarding-Plan, bevor er antwortet; Sales Copilot kennt den
ICP-Score, den der SDR-Agent ermittelt hat).

Analog zu core/consent.py (Ledger-Muster) und tools/sequence_scheduler.py:
Prozessweiter In-Memory-Singleton (`_store`), Einträge gehen bei Neustart
verloren. TODO vor Produktivbetrieb: persistenter Store (Postgres/Redis),
identisches Interface.

Identifier-Auflösung: Kunden werden über eine normalisierte E-Mail-Adresse
identifiziert (bevorzugt), mit dem Firmennamen als Fallback, wenn keine
E-Mail bekannt ist (z. B. Operations-Agent verarbeitet Rechnungen ohne
Kontakt-Mail; Sales Copilot erfasst aktuell keine Kontakt-E-Mail). Das ist
eine bewusste Vereinfachung fürs Mock-System — in Produktion braucht es eine
echte Kunden-ID (CRM-Primärschlüssel), die E-Mail und Firmenname zuverlässig
zusammenführt, siehe "Bekannte Einschränkungen" in CLAUDE.md. Ohne jeden
Identifier (weder E-Mail noch Firmenname bekannt) wird ein update_stage()-
Aufruf bewusst zum No-op statt zu einem Fehler — ein Agent, der (noch) keinen
Kundenbezug herstellen kann, soll dadurch nicht abbrechen.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Stage = Literal["sdr", "sales_copilot", "onboarding", "support", "operations"]

_STAGE_ORDER: tuple[Stage, ...] = (
    "sdr", "sales_copilot", "onboarding", "support", "operations",
)


def _normalize(identifier: str) -> str:
    return identifier.strip().lower()


class StageSnapshot(BaseModel):
    """Letzter bekannter Zustand eines Agenten für einen Kunden."""

    stage: Stage
    agent_session_id: str
    data: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CustomerState(BaseModel):
    """Voller, über alle 5 Agenten geteilter Kundenzustand."""

    customer_id: str  # normalisierter Identifier (E-Mail bevorzugt, sonst Firmenname)
    company_name: Optional[str] = None
    primary_email: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    stages: dict[Stage, StageSnapshot] = Field(default_factory=dict)

    @property
    def journey(self) -> list[Stage]:
        """Stufen, die dieser Kunde bereits durchlaufen hat, in Ausführungsreihenfolge."""
        return [s for s in _STAGE_ORDER if s in self.stages]


class StageEvent(BaseModel):
    """Ein einzelner Audit-Trail-Eintrag — Events werden nie überschrieben, nur angehängt."""

    customer_id: str
    stage: Stage
    agent_session_id: str
    recorded_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CustomerStateStore:
    """
    Geteilter Kundenzustand, indiziert über einen normalisierten Identifier.

    `_snapshots` hält pro Kunde den aktuellen Stand jeder Stufe (schneller
    Lesezugriff für nachgelagerte Agenten). `_history` ist Append-only
    (voller Audit-Trail, gleiches Muster wie core/consent.py).
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, CustomerState] = {}
        self._history: list[StageEvent] = []
        # normalisierter Firmenname -> customer_id. Nötig, weil nicht jeder
        # Agent eine E-Mail kennt (Sales Copilot, Operations) -- ohne diesen
        # Index würde ein firmennamen-only-Aufruf für eine Firma, die
        # bereits unter ihrer E-Mail bekannt ist (z. B. vom SDR-Agent
        # angelegt), einen zweiten, getrennten Kunden-Eintrag erzeugen statt
        # in den bestehenden zu mergen.
        self._company_index: dict[str, str] = {}

    def _resolve_identifier(self, email: Optional[str], company_name: Optional[str]) -> Optional[str]:
        if email:
            return _normalize(email)
        if company_name:
            norm_company = _normalize(company_name)
            # Ein bereits über E-Mail identifizierter Kunde mit demselben
            # Firmennamen hat Vorrang vor einem neuen, rein
            # firmennamen-basierten Identifier.
            return self._company_index.get(norm_company, norm_company)
        return None

    def update_stage(
        self,
        stage: Stage,
        data: dict[str, Any],
        *,
        email: Optional[str] = None,
        company_name: Optional[str] = None,
        agent_session_id: str = "unknown",
    ) -> Optional[CustomerState]:
        """
        Schreibt einen Stage-Snapshot für den Kunden, der über `email`
        (bevorzugt) oder `company_name` identifiziert wird. Gibt None zurück
        (kein Fehler), wenn weder E-Mail noch Firmenname bekannt sind — es
        gibt dann nichts, worüber sich State teilen ließe.
        """
        customer_id = self._resolve_identifier(email, company_name)
        if customer_id is None:
            logger.debug("Customer state skipped -- no identifier", extra={"stage": stage})
            return None

        state = self._snapshots.get(customer_id)
        if state is None:
            state = CustomerState(
                customer_id=customer_id,
                company_name=company_name,
                primary_email=_normalize(email) if email else None,
            )
            self._snapshots[customer_id] = state
        else:
            # Ergänzt bislang unbekannte Stammdaten, überschreibt nie einen
            # bereits bekannten Wert mit None/leer.
            if company_name and not state.company_name:
                state.company_name = company_name
            if email and not state.primary_email:
                state.primary_email = _normalize(email)

        if company_name:
            self._company_index[_normalize(company_name)] = customer_id

        state.stages[stage] = StageSnapshot(stage=stage, agent_session_id=agent_session_id, data=data)
        self._history.append(
            StageEvent(customer_id=customer_id, stage=stage, agent_session_id=agent_session_id)
        )
        logger.info(
            "Customer state updated",
            extra={"customer_id": customer_id, "stage": stage, "session_id": agent_session_id},
        )
        return state

    def get(
        self, *, email: Optional[str] = None, company_name: Optional[str] = None
    ) -> Optional[CustomerState]:
        customer_id = self._resolve_identifier(email, company_name)
        if customer_id is None:
            return None
        return self._snapshots.get(customer_id)

    def get_stage(
        self,
        stage: Stage,
        *,
        email: Optional[str] = None,
        company_name: Optional[str] = None,
    ) -> Optional[StageSnapshot]:
        state = self.get(email=email, company_name=company_name)
        return state.stages.get(stage) if state else None

    def history(self, customer_id: Optional[str] = None) -> list[StageEvent]:
        """Vollständiger Audit-Trail, optional gefiltert nach normalisiertem Kunden-Identifier."""
        entries = self._history
        if customer_id is not None:
            norm = _normalize(customer_id)
            entries = [e for e in entries if e.customer_id == norm]
        return list(entries)

    def all_customers(self) -> list[CustomerState]:
        """Nur für Tests/Dev -- alle bekannten Kunden."""
        return list(self._snapshots.values())


# Prozessweiter Singleton -- von allen 5 Agenten geteilt (analog zu
# core.consent._ledger), damit ein Snapshot, den ein Agent schreibt, für
# einen später aufgerufenen Agenten in derselben Kunden-Journey sichtbar ist.
_store = CustomerStateStore()


def update_stage(
    stage: Stage,
    data: dict[str, Any],
    *,
    email: Optional[str] = None,
    company_name: Optional[str] = None,
    agent_session_id: str = "unknown",
) -> Optional[CustomerState]:
    return _store.update_stage(
        stage, data, email=email, company_name=company_name, agent_session_id=agent_session_id
    )


def get(*, email: Optional[str] = None, company_name: Optional[str] = None) -> Optional[CustomerState]:
    return _store.get(email=email, company_name=company_name)


def get_stage(
    stage: Stage, *, email: Optional[str] = None, company_name: Optional[str] = None
) -> Optional[StageSnapshot]:
    return _store.get_stage(stage, email=email, company_name=company_name)


def history(customer_id: Optional[str] = None) -> list[StageEvent]:
    return _store.history(customer_id)


def all_customers() -> list[CustomerState]:
    return _store.all_customers()
