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

Prozessweiter In-Memory-Singleton (`_scheduler`), gleiches Muster wie
core/consent.py und die Mock-Stores in tools/crm_integration.py — Sequenzen
gehen bei Neustart verloren. TODO vor Produktivbetrieb: persistenter Store
(Postgres/Redis) + echter Worker, identische Interface-Methoden.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core import consent
from core.consent import Channel

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


class SequenceScheduler:
    """Auditierbare Multi-Touch-Kadenz mit Retry-Logik pro Schritt."""

    def __init__(self) -> None:
        self._sequences: dict[str, Sequence] = {}
        self._by_identifier: dict[str, str] = {}  # normalisierter Identifier -> sequence_id

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

        seq = Sequence(
            sequence_id=f"seq-{uuid.uuid4().hex[:10]}",
            lead_key=lead_key,
            identifiers=dict(identifiers),
            steps=steps,
        )
        self._sequences[seq.sequence_id] = seq
        for identifier in identifiers.values():
            if identifier:
                self._by_identifier[_normalize(identifier)] = seq.sequence_id

        # Schritt 0 (first_channel) wurde bereits versucht -- Ergebnis direkt
        # verbuchen, damit ein Fehlschlag sofort in die Retry-Logik einfließt.
        self.record_attempt(seq.sequence_id, 0, success=first_success, reason=first_reason)

        # Übrige Schritte: Identifier-/Consent-Check, bevor überhaupt versucht wird.
        for step in seq.steps[1:]:
            identifier = identifiers.get(step.channel)
            if not identifier:
                step.status = "skipped"
                step.last_reason = "kein Identifier für diesen Kanal bekannt"
            elif not consent.is_allowed(identifier, step.channel):
                step.status = "skipped"
                step.last_reason = "Opt-out für diesen Kanal hinterlegt"
        self._advance(seq)

        logger.info(
            "Sequence enrolled",
            extra={"sequence_id": seq.sequence_id, "lead_key": lead_key, "first_channel": first_channel},
        )
        return seq

    def record_attempt(self, sequence_id: str, step_index: int, success: bool, reason: str = "") -> StepRecord:
        """
        Zeichnet das Ergebnis EINES Versuchs auf. Bei Erfolg: Status "sent".
        Bei Fehlschlag: solange attempts <= max_retries bleibt der Schritt
        "pending" (ein künftiger Worker kann erneut versuchen); danach
        "failed", und die Kadenz rückt automatisch zum nächsten Schritt vor.
        """
        seq = self._sequences[sequence_id]
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
        seq = self._sequences[sequence_id]
        if seq.status != "active" or seq.current_step >= len(seq.steps):
            return None
        return seq.current_step, seq.steps[seq.current_step]

    def stop(self, sequence_id: str, reason: str) -> Sequence:
        """Wird vom Reply-Classifier aufgerufen: Interesse oder Opt-out beendet die Kadenz sofort."""
        seq = self._sequences[sequence_id]
        seq.status = "stopped"
        seq.stopped_reason = reason
        logger.info("Sequence stopped", extra={"sequence_id": sequence_id, "reason": reason})
        return seq

    def get(self, sequence_id: str) -> Optional[Sequence]:
        return self._sequences.get(sequence_id)

    def find_by_identifier(self, identifier: Optional[str]) -> Optional[Sequence]:
        if not identifier:
            return None
        sequence_id = self._by_identifier.get(_normalize(identifier))
        return self._sequences.get(sequence_id) if sequence_id else None


# Prozessweiter Singleton — vom SDR-Agent (enroll) und vom Inbound-Reply-
# Webhook (find_by_identifier, stop) gemeinsam genutzt.
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


def next_due_step(sequence_id: str) -> Optional[tuple[int, StepRecord]]:
    return _scheduler.next_due_step(sequence_id)


def stop(sequence_id: str, reason: str) -> Sequence:
    return _scheduler.stop(sequence_id, reason)


def get(sequence_id: str) -> Optional[Sequence]:
    return _scheduler.get(sequence_id)


def find_by_identifier(identifier: Optional[str]) -> Optional[Sequence]:
    return _scheduler.find_by_identifier(identifier)
