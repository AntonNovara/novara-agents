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

Persistiert über core/db.py (Postgres in Produktion, SQLite lokal) seit
21.09.2026 — vorher In-Memory-Prozess-Singleton (analog zu core/consent.py),
Einträge gingen bei jedem Neustart verloren. Alle öffentlichen Methoden
(update_stage, get, get_stage, history, all_customers) sind unverändert;
`stages` (pro Kunde) liegt als JSON-Spalte in `customer_snapshots`, der
Audit-Trail in einer eigenen `customer_stage_events`-Tabelle, der
Firmenname->Kunde-Index in `customer_company_index`.

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
from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base, SessionLocal, engine

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


class _CustomerRow(Base):
    __tablename__ = "customer_snapshots"

    customer_id: Mapped[str] = mapped_column(String(320), primary_key=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    primary_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # dict[Stage, dict] (StageSnapshot.model_dump()) -- ein JSON-Blob statt
    # einer eigenen Tabelle pro Stage: `stages` wird immer als Ganzes
    # gelesen/geschrieben (get()/get_stage() lesen aus demselben Snapshot,
    # nie stufenübergreifend gefiltert/sortiert), eine normalisierte Tabelle
    # hätte hier nur Joins ohne echten Nutzen hinzugefügt.
    stages: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class _StageEventRow(Base):
    __tablename__ = "customer_stage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class _CompanyIndexRow(Base):
    __tablename__ = "customer_company_index"

    company_name_norm: Mapped[str] = mapped_column(String(255), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(320), nullable=False)


# Nur diese drei Tabellen (siehe core/consent.py für die Begründung) --
# idempotent, funktioniert auch, wenn dieses Modul isoliert importiert wird.
Base.metadata.create_all(
    bind=engine, tables=[_CustomerRow.__table__, _StageEventRow.__table__, _CompanyIndexRow.__table__]
)


def _row_to_state(row: _CustomerRow) -> CustomerState:
    return CustomerState(
        customer_id=row.customer_id,
        company_name=row.company_name,
        primary_email=row.primary_email,
        created_at=row.created_at.isoformat(),
        stages={stage: StageSnapshot.model_validate(snap) for stage, snap in row.stages.items()},
    )


class CustomerStateStore:
    """
    Geteilter Kundenzustand, indiziert über einen normalisierten Identifier.

    Jede Methode öffnet/schließt ihre eigene kurzlebige DB-Session (gleiches
    Muster wie core/consent.py). `_resolve_identifier()` prüft zuerst den
    Firmennamen-Index (`customer_company_index`), damit ein bereits über
    E-Mail bekannter Kunde nicht durch einen späteren, rein
    firmennamen-basierten Aufruf verdoppelt wird.
    """

    def _resolve_identifier(
        self, session, email: Optional[str], company_name: Optional[str]
    ) -> Optional[str]:
        if email:
            return _normalize(email)
        if company_name:
            norm_company = _normalize(company_name)
            indexed = session.get(_CompanyIndexRow, norm_company)
            return indexed.customer_id if indexed else norm_company
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
        with SessionLocal() as session:
            customer_id = self._resolve_identifier(session, email, company_name)
            if customer_id is None:
                logger.debug("Customer state skipped -- no identifier", extra={"stage": stage})
                return None

            row = session.get(_CustomerRow, customer_id)
            if row is None:
                row = _CustomerRow(
                    customer_id=customer_id,
                    company_name=company_name,
                    primary_email=_normalize(email) if email else None,
                    created_at=datetime.now(timezone.utc),
                    stages={},
                )
                session.add(row)
            else:
                # Ergänzt bislang unbekannte Stammdaten, überschreibt nie
                # einen bereits bekannten Wert mit None/leer.
                if company_name and not row.company_name:
                    row.company_name = company_name
                if email and not row.primary_email:
                    row.primary_email = _normalize(email)

            if company_name:
                norm_company = _normalize(company_name)
                idx = session.get(_CompanyIndexRow, norm_company)
                if idx is None:
                    session.add(_CompanyIndexRow(company_name_norm=norm_company, customer_id=customer_id))
                else:
                    idx.customer_id = customer_id

            snapshot = StageSnapshot(stage=stage, agent_session_id=agent_session_id, data=data)
            # Neues dict zuweisen statt in-place zu mutieren -- SQLAlchemys
            # Change-Tracking für JSON-Spalten erkennt eine Mutation des
            # bestehenden dict-Objekts sonst nicht zuverlässig als "dirty".
            row.stages = {**row.stages, stage: snapshot.model_dump()}

            session.add(
                _StageEventRow(
                    customer_id=customer_id,
                    stage=stage,
                    agent_session_id=agent_session_id,
                    recorded_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
            session.refresh(row)
            state = _row_to_state(row)

        logger.info(
            "Customer state updated",
            extra={"customer_id": customer_id, "stage": stage, "session_id": agent_session_id},
        )
        return state

    def get(
        self, *, email: Optional[str] = None, company_name: Optional[str] = None
    ) -> Optional[CustomerState]:
        with SessionLocal() as session:
            customer_id = self._resolve_identifier(session, email, company_name)
            if customer_id is None:
                return None
            row = session.get(_CustomerRow, customer_id)
            return _row_to_state(row) if row else None

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
        with SessionLocal() as session:
            query = session.query(_StageEventRow)
            if customer_id is not None:
                query = query.filter_by(customer_id=_normalize(customer_id))
            rows = query.order_by(_StageEventRow.id.asc()).all()
            return [
                StageEvent(
                    customer_id=r.customer_id,
                    stage=r.stage,  # type: ignore[arg-type]
                    agent_session_id=r.agent_session_id,
                    recorded_at=r.recorded_at.isoformat(),
                )
                for r in rows
            ]

    def all_customers(self) -> list[CustomerState]:
        """Nur für Tests/Dev -- alle bekannten Kunden."""
        with SessionLocal() as session:
            rows = session.query(_CustomerRow).all()
            return [_row_to_state(r) for r in rows]


# Prozessweiter Singleton -- von allen 5 Agenten geteilt (analog zu
# core.consent._ledger). Hält selbst keine Daten mehr, siehe
# CustomerStateStore-Docstring.
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
