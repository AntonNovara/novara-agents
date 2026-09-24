"""
Sequence Scheduler – kanalübergreifende Multi-Touch-Kadenz mit konfigurierbaren
Wiederholungen.

Modelliert, welcher Kanal (E-Mail, LinkedIn, Anruf) an welchem Tag versucht
wird und wie oft ein fehlgeschlagener Versuch wiederholt wird, bevor die
Kadenz zum nächsten Kanal übergeht. Führt selbst NICHTS zeitgesteuert aus —
es gibt hier (noch) keinen echten Scheduler/Cron-Worker, der attempt_step()
am jeweils fälligen Tag automatisch aufruft. In Produktion würde ein Worker
(Celery Beat / APScheduler / Cron) periodisch die aktiven Sequenzen abfragen
und für jeden fälligen Schritt attempt_step()/record_attempt() aufrufen.

Der "voice"-Kanal wird nie automatisch versucht: agents/voice_agent.py nimmt
ausschließlich eingehende Anrufe entgegen, es gibt keinen ausgehenden Dialer
in diesem Repo (siehe CLAUDE.md, Abschnitt "Voice Agent"). Ein "voice"-Schritt
bleibt daher immer "skipped", solange kein Identifier (Telefonnummer) bekannt
ist — was heute für jeden Lead zutrifft, da ProspectContact/LeadRecord aktuell
keine Telefonnummer erfassen.

Persistiert über core/db.py (Postgres in Produktion, SQLite lokal) seit
21.09.2026 — vorher In-Memory-Prozess-Singleton (analog zu core/consent.py),
Sequenzen gingen bei jedem Neustart verloren (der eigentliche Worker fehlt
weiterhin, siehe Absatz oben — das war nie Teil dieser Runde). Alle
öffentlichen Methoden (enroll, record_attempt, next_due_step, stop, get,
find_by_identifier) sind unverändert, inklusive KeyError bei unbekannter
sequence_id (vorher `self._sequences[sequence_id]`, jetzt explizit geprüft).
`Sequence`/`StepRecord` bleiben dieselben Dataclasses wie vorher — eine ganze
Sequenz (Steps + Identifiers) liegt als EIN JSON-Blob pro Zeile
(`sequences`-Tabelle), keine eigene Tabelle pro Step: jeder Lese-/
Schreibzugriff braucht immer die vollständige, geordnete Step-Liste (nie nur
einzelne Steps gefiltert/sortiert), eine normalisierte Tabelle hätte hier nur
Joins ohne echten Nutzen hinzugefügt — gleiche Abwägung wie
core/customer_state.py's `stages`-Spalte.
"""
from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core import consent
from core.consent import Channel
from core.db import Base, SessionLocal, engine

logger = logging.getLogger(__name__)

StepStatus = str  # "pending" | "sent" | "failed" | "skipped"

# Reihenfolge + Standard-Wiederholungen pro Kanal, wenn er NICHT der bereits
# versendete Erstkontakt ist (siehe enroll()). day_offset ist beschreibende
# Metadaten für einen künftigen echten Worker, wird hier nicht durchgesetzt.
_FOLLOWUP_ORDER: tuple[tuple[Channel, int, int], ...] = (
    ("email", 3, 2),
    ("linkedin", 5, 1),
    ("voice", 9, 0),
)


def _normalize(identifier: str) -> str:
    return identifier.strip().lower()


@dataclass
class StepRecord:
    channel: Channel
    day_offset: int
    max_retries: int
    status: StepStatus = "pending"
    attempts: int = 0
    last_reason: str = ""
    last_attempt_at: Optional[str] = None


@dataclass
class Sequence:
    sequence_id: str
    lead_key: str
    identifiers: dict[Channel, Optional[str]]
    steps: list[StepRecord]
    current_step: int = 0
    status: str = "active"  # active | completed | stopped
    stopped_reason: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class _SequenceRow(Base):
    __tablename__ = "sequences"

    sequence_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    lead_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    stopped_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    identifiers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # list[dict] -- StepRecord.__dict__ pro Step, siehe Moduldocstring.
    steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class _SequenceIdentifierIndexRow(Base):
    __tablename__ = "sequence_identifier_index"

    identifier_norm: Mapped[str] = mapped_column(String(320), primary_key=True)
    sequence_id: Mapped[str] = mapped_column(String(32), nullable=False)


# Nur diese beiden Tabellen (siehe core/consent.py für die Begründung).
Base.metadata.create_all(bind=engine, tables=[_SequenceRow.__table__, _SequenceIdentifierIndexRow.__table__])


def _row_to_sequence(row: _SequenceRow) -> Sequence:
    return Sequence(
        sequence_id=row.sequence_id,
        lead_key=row.lead_key,
        identifiers=dict(row.identifiers),
        steps=[StepRecord(**step) for step in row.steps],
        current_step=row.current_step,
        status=row.status,
        stopped_reason=row.stopped_reason,
        created_at=row.created_at.isoformat(),
    )


def _steps_to_json(steps: list[StepRecord]) -> list[dict]:
    return [dataclasses.asdict(s) for s in steps]


class SequenceScheduler:
    """Auditierbare Multi-Touch-Kadenz mit Retry-Logik pro Schritt."""

    def enroll(
        self,
        lead_key: str,
        identifiers: dict[Channel, Optional[str]],
        first_channel: Channel,
        first_success: bool,
        first_reason: str = "",
    ) -> Sequence:
        """
        Registriert einen Lead für die Kadenz. `first_channel` ist der Kanal,
        über den compose_outreach()/write_to_crm() den ERSTEN Touch bereits
        erzeugt hat — dessen Ergebnis (first_success) fließt sofort in die
        Retry-Logik ein (ein CRM-Schreibfehler zählt als fehlgeschlagener
        Versuch, kein stiller Erfolg). Die restlichen Kanäle folgen in
        _FOLLOWUP_ORDER; Schritte ohne bekannten Identifier oder mit einem
        Opt-out für ihren Kanal werden sofort als "skipped" markiert, statt
        als "pending" hängen zu bleiben.
        """
        followups = [(ch, day, retries) for ch, day, retries in _FOLLOWUP_ORDER if ch != first_channel]
        steps = [StepRecord(channel=first_channel, day_offset=0, max_retries=2)]
        steps.extend(
            StepRecord(channel=ch, day_offset=day, max_retries=retries) for ch, day, retries in followups
        )

        sequence_id = f"seq-{uuid.uuid4().hex[:10]}"
        with SessionLocal() as session:
            session.add(
                _SequenceRow(
                    sequence_id=sequence_id,
                    lead_key=lead_key,
                    status="active",
                    stopped_reason="",
                    created_at=datetime.now(timezone.utc),
                    current_step=0,
                    identifiers=dict(identifiers),
                    steps=_steps_to_json(steps),
                )
            )
            for identifier in identifiers.values():
                if not identifier:
                    continue
                norm = _normalize(identifier)
                idx = session.get(_SequenceIdentifierIndexRow, norm)
                if idx is None:
                    session.add(_SequenceIdentifierIndexRow(identifier_norm=norm, sequence_id=sequence_id))
                else:
                    idx.sequence_id = sequence_id
            session.commit()

        # Schritt 0 (first_channel) wurde bereits versucht -- Ergebnis direkt
        # verbuchen, damit ein Fehlschlag sofort in die Retry-Logik einfließt.
        self.record_attempt(sequence_id, 0, success=first_success, reason=first_reason)

        # Übrige Schritte: Identifier-/Consent-Check, bevor überhaupt versucht wird.
        with SessionLocal() as session:
            row = session.get(_SequenceRow, sequence_id)
            seq = _row_to_sequence(row)
            for step in seq.steps[1:]:
                identifier = identifiers.get(step.channel)
                if not identifier:
                    step.status = "skipped"
                    step.last_reason = "kein Identifier für diesen Kanal bekannt"
                elif not consent.is_allowed(identifier, step.channel):
                    step.status = "skipped"
                    step.last_reason = "Opt-out für diesen Kanal hinterlegt"
            self._advance(seq)

            row.steps = _steps_to_json(seq.steps)
            row.current_step = seq.current_step
            row.status = seq.status
            session.commit()
            session.refresh(row)
            result = _row_to_sequence(row)

        logger.info(
            "Sequence enrolled",
            extra={"sequence_id": sequence_id, "lead_key": lead_key, "first_channel": first_channel},
        )
        return result

    def record_attempt(self, sequence_id: str, step_index: int, success: bool, reason: str = "") -> StepRecord:
        """
        Zeichnet das Ergebnis EINES Versuchs auf. Bei Erfolg: Status "sent".
        Bei Fehlschlag: solange attempts <= max_retries bleibt der Schritt
        "pending" (ein künftiger Worker kann erneut versuchen); danach
        "failed", und die Kadenz rückt automatisch zum nächsten Schritt vor.
        """
        with SessionLocal() as session:
            row = session.get(_SequenceRow, sequence_id)
            if row is None:
                raise KeyError(sequence_id)
            seq = _row_to_sequence(row)
            step = seq.steps[step_index]
            step.attempts += 1
            step.last_reason = reason
            step.last_attempt_at = datetime.now(timezone.utc).isoformat()
            if success:
                step.status = "sent"
            elif step.attempts > step.max_retries:
                step.status = "failed"
            # sonst bleibt "pending" -- Retry möglich, current_step rückt NICHT vor
            self._advance(seq)

            row.steps = _steps_to_json(seq.steps)
            row.current_step = seq.current_step
            row.status = seq.status
            session.commit()
        return step

    def _advance(self, seq: Sequence) -> None:
        while seq.current_step < len(seq.steps) and seq.steps[seq.current_step].status in (
            "sent", "failed", "skipped",
        ):
            seq.current_step += 1
        if seq.current_step >= len(seq.steps) and seq.status == "active":
            seq.status = "completed"

    def next_due_step(self, sequence_id: str) -> Optional[tuple[int, StepRecord]]:
        """Für einen künftigen Worker: der nächste Schritt, der noch aussteht, falls die Sequenz aktiv ist."""
        with SessionLocal() as session:
            row = session.get(_SequenceRow, sequence_id)
            if row is None:
                raise KeyError(sequence_id)
            seq = _row_to_sequence(row)
        if seq.status != "active" or seq.current_step >= len(seq.steps):
            return None
        return seq.current_step, seq.steps[seq.current_step]

    def list_due(self, now: Optional[datetime] = None) -> list[dict]:
        """
        Alle aktiven Sequenzen, deren AKTUELLER Schritt noch aussteht UND laut
        Kadenz fällig ist (created_at + day_offset Tage <= now). Für den
        täglichen Follow-up-Digest (main.py /api/v1/internal/sequences/*):
        Novara verschickt Follow-ups bewusst NICHT automatisch -- der Digest
        zeigt Anton, was ansteht, und er sendet mit Augenmaß selbst.
        """
        now = now or datetime.now(timezone.utc)
        with SessionLocal() as session:
            rows = session.query(_SequenceRow).filter(_SequenceRow.status == "active").all()
            sequences = [_row_to_sequence(r) for r in rows]
        due: list[dict] = []
        for seq in sequences:
            if seq.current_step >= len(seq.steps):
                continue
            step = seq.steps[seq.current_step]
            if step.status != "pending":
                continue
            due_at = datetime.fromisoformat(seq.created_at) + timedelta(days=step.day_offset)
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
            if due_at <= now:
                due.append({
                    "sequence_id": seq.sequence_id,
                    "lead_key": seq.lead_key,
                    "channel": step.channel,
                    "step_index": seq.current_step,
                    "day_offset": step.day_offset,
                    "due_since": due_at.isoformat(),
                    "identifier": seq.identifiers.get(step.channel),
                    "attempts": step.attempts,
                })
        return sorted(due, key=lambda d: d["due_since"])

    def stop(self, sequence_id: str, reason: str) -> Sequence:
        """Wird vom Reply-Classifier aufgerufen: Interesse oder Opt-out beendet die Kadenz sofort."""
        with SessionLocal() as session:
            row = session.get(_SequenceRow, sequence_id)
            if row is None:
                raise KeyError(sequence_id)
            row.status = "stopped"
            row.stopped_reason = reason
            session.commit()
            session.refresh(row)
            result = _row_to_sequence(row)
        logger.info("Sequence stopped", extra={"sequence_id": sequence_id, "reason": reason})
        return result

    def get(self, sequence_id: str) -> Optional[Sequence]:
        with SessionLocal() as session:
            row = session.get(_SequenceRow, sequence_id)
            return _row_to_sequence(row) if row else None

    def find_by_identifier(self, identifier: Optional[str]) -> Optional[Sequence]:
        if not identifier:
            return None
        with SessionLocal() as session:
            idx = session.get(_SequenceIdentifierIndexRow, _normalize(identifier))
            if idx is None:
                return None
            row = session.get(_SequenceRow, idx.sequence_id)
            return _row_to_sequence(row) if row else None


# Prozessweiter Singleton — vom SDR-Agent (enroll) und vom Inbound-Reply-
# Webhook (find_by_identifier, stop) gemeinsam genutzt. Hält selbst keine
# Daten mehr, siehe SequenceScheduler-Docstring.
_scheduler = SequenceScheduler()


def enroll(
    lead_key: str,
    identifiers: dict[Channel, Optional[str]],
    first_channel: Channel,
    first_success: bool,
    first_reason: str = "",
) -> Sequence:
    return _scheduler.enroll(lead_key, identifiers, first_channel, first_success, first_reason)


def record_attempt(sequence_id: str, step_index: int, success: bool, reason: str = "") -> StepRecord:
    return _scheduler.record_attempt(sequence_id, step_index, success, reason)


def list_due(now: Optional[datetime] = None) -> list[dict]:
    return _scheduler.list_due(now)


def next_due_step(sequence_id: str) -> Optional[tuple[int, StepRecord]]:
    return _scheduler.next_due_step(sequence_id)


def stop(sequence_id: str, reason: str) -> Sequence:
    return _scheduler.stop(sequence_id, reason)


def get(sequence_id: str) -> Optional[Sequence]:
    return _scheduler.get(sequence_id)


def find_by_identifier(identifier: Optional[str]) -> Optional[Sequence]:
    return _scheduler.find_by_identifier(identifier)
