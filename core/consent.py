"""
Consent-Ledger – Opt-in/Opt-out-Register pro Kontakt-Identifier und Kanal.

DSGVO Art. 7 (Nachweisbarkeit der Einwilligung) + ePrivacy-Richtlinie: jede
Kaltakquise-Aktion (E-Mail, Anruf, LinkedIn-Nachricht) braucht eine
nachvollziehbare Rechtsgrundlage, und ein Widerspruch muss respektiert UND
auditierbar protokolliert werden (DSGVO Art. 30). Dieses Modul ist das
zentrale Register dafür — Agenten fragen hier VOR jeder Outreach-Aktion an,
ob der jeweilige Kontakt über den jeweiligen Kanal kontaktiert werden darf.

Analog zu core/security.py: die Prüfung ist deterministisch, kein LLM ist an
der Entscheidung beteiligt. Ein Opt-out blockt IMMER — unabhängig davon, was
ein Agent sonst "denkt" oder generiert.

Persistiert über core/db.py (Postgres in Produktion, SQLite lokal) seit
21.09.2026 — vorher In-Memory-Prozess-Singleton, Einträge gingen bei jedem
Neustart verloren. `ConsentLedger`s öffentliche Methoden (is_allowed,
record_opt_out, record_opt_in, status, history) sind unverändert; nur die
Speicherung dahinter wechselte von zwei Dicts auf eine `consent_records`-
Tabelle, damit kein Aufrufer (core/consent.py hat einige, siehe
agents/sdr_agent.py check_consent) angepasst werden musste.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base, SessionLocal, engine

logger = logging.getLogger(__name__)

Channel = Literal["email", "voice", "linkedin"]
ConsentStatus = Literal["opt_in", "opt_out"]


class ConsentRecord(BaseModel):
    """Ein einzelner Audit-Trail-Eintrag — Einträge werden nie überschrieben, nur angehängt."""

    identifier: str  # normalisierte Kontakt-Adresse: E-Mail, Telefonnummer oder LinkedIn-URL
    channel: Channel
    status: ConsentStatus
    reason: str
    recorded_by: str = "system"
    recorded_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class _ConsentRow(Base):
    """DB-Zeile hinter einem ConsentRecord — append-only, siehe ConsentLedger."""

    __tablename__ = "consent_records"
    __table_args__ = (Index("ix_consent_identifier_channel", "identifier", "channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identifier: Mapped[str] = mapped_column(String(320), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recorded_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Erzeugt NUR diese Tabelle (tables=[...], nicht Base.metadata.create_all()
# ohne Filter) -- ein Aufrufer, der nur core.consent importiert (z. B.
# test_system.py, das die App-Lifespan von main.py nie durchläuft), braucht
# nicht zusätzlich core.db.init_db() aufzurufen. Idempotent (CREATE TABLE IF
# NOT EXISTS-Semantik), main.py's init_db() beim Start ist dadurch redundant,
# aber als expliziter, zentraler Schritt bewusst beibehalten.
Base.metadata.create_all(bind=engine, tables=[_ConsentRow.__table__])


def _row_to_record(row: _ConsentRow) -> ConsentRecord:
    return ConsentRecord(
        identifier=row.identifier,
        channel=row.channel,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        reason=row.reason,
        recorded_by=row.recorded_by,
        recorded_at=row.recorded_at.isoformat(),
    )


def _normalize(identifier: str) -> str:
    return identifier.strip().lower()


class ConsentLedger:
    """
    Auditierbares Opt-in/Opt-out-Register.

    Jede Methode öffnet/schließt ihre eigene kurzlebige DB-Session (gleiches
    Muster in allen vier core/db.py-Nutzern) — kein Zustand wird zwischen
    Aufrufen im Prozess gehalten, ausschließlich in der `consent_records`-
    Tabelle. `status()`/`is_allowed()` lesen die jeweils NEUESTE Zeile pro
    (Identifier, Kanal) über `ORDER BY id DESC LIMIT 1` — die aufsteigende,
    autoincrementierte `id` ist dabei zuverlässiger als `recorded_at` als
    Sortierschlüssel (zwei Einträge in derselben Millisekunde wären sonst
    nicht eindeutig ordbar).
    """

    def _record(
        self, identifier: str, channel: Channel, status: ConsentStatus, reason: str, recorded_by: str
    ) -> ConsentRecord:
        key_id = _normalize(identifier)
        with SessionLocal() as session:
            row = _ConsentRow(
                identifier=key_id,
                channel=channel,
                status=status,
                reason=reason,
                recorded_by=recorded_by,
                recorded_at=datetime.now(timezone.utc),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            entry = _row_to_record(row)
        logger.info(
            "Consent recorded",
            extra={"channel": channel, "status": status, "reason": reason, "recorded_by": recorded_by},
        )
        return entry

    def record_opt_out(
        self, identifier: str, channel: Channel, reason: str, recorded_by: str = "system"
    ) -> ConsentRecord:
        return self._record(identifier, channel, "opt_out", reason, recorded_by)

    def record_opt_in(
        self, identifier: str, channel: Channel, reason: str, recorded_by: str = "system"
    ) -> ConsentRecord:
        return self._record(identifier, channel, "opt_in", reason, recorded_by)

    def status(self, identifier: str, channel: Channel) -> Optional[ConsentStatus]:
        """None = kein Eintrag vorhanden (weder Opt-in noch Opt-out bekannt)."""
        key_id = _normalize(identifier)
        with SessionLocal() as session:
            row = (
                session.query(_ConsentRow)
                .filter_by(identifier=key_id, channel=channel)
                .order_by(_ConsentRow.id.desc())
                .first()
            )
            return row.status if row else None  # type: ignore[return-value]

    def is_allowed(self, identifier: Optional[str], channel: Channel) -> bool:
        """
        Darf dieser Kontakt über diesen Kanal kontaktiert werden?

        Ohne Identifier (z. B. unbekannte Adresse) gibt es nichts zu prüfen —
        Default erlaubt, damit ein fehlender Identifier nicht versehentlich
        den gesamten Outreach-Flow blockiert. Ein bekannter Opt-out blockt
        IMMER; kein Eintrag oder ein Opt-in erlaubt (berechtigtes Interesse
        ist im B2B-Kaltakquise-Kontext die übliche Rechtsgrundlage, DSGVO
        Erwägungsgrund 47 — explizites Opt-in wird respektiert, wo es
        vorliegt, ist aber keine Voraussetzung für den ersten Kontakt).
        """
        if not identifier:
            return True
        return self.status(identifier, channel) != "opt_out"

    def history(
        self, identifier: Optional[str] = None, channel: Optional[Channel] = None
    ) -> list[ConsentRecord]:
        """Vollständiger Audit-Trail, optional gefiltert nach Kontakt und/oder Kanal."""
        with SessionLocal() as session:
            query = session.query(_ConsentRow)
            if identifier is not None:
                query = query.filter_by(identifier=_normalize(identifier))
            if channel is not None:
                query = query.filter_by(channel=channel)
            rows = query.order_by(_ConsentRow.id.asc()).all()
            return [_row_to_record(r) for r in rows]


# Prozessweiter Singleton — von allen Agenten geteilt (analog zu
# core.config.settings). Hält selbst keine Daten mehr (siehe ConsentLedger-
# Docstring), bleibt aber als Modul-Singleton bestehen, damit ein künftiges
# In-Process-Caching (falls je gebraucht) an einer Stelle ansetzen könnte.
_ledger = ConsentLedger()


def record_opt_out(
    identifier: str, channel: Channel, reason: str, recorded_by: str = "system"
) -> ConsentRecord:
    return _ledger.record_opt_out(identifier, channel, reason, recorded_by)


def record_opt_in(
    identifier: str, channel: Channel, reason: str, recorded_by: str = "system"
) -> ConsentRecord:
    return _ledger.record_opt_in(identifier, channel, reason, recorded_by)


def is_allowed(identifier: Optional[str], channel: Channel) -> bool:
    return _ledger.is_allowed(identifier, channel)


def status(identifier: str, channel: Channel) -> Optional[ConsentStatus]:
    return _ledger.status(identifier, channel)


def history(identifier: Optional[str] = None, channel: Optional[Channel] = None) -> list[ConsentRecord]:
    return _ledger.history(identifier, channel)
