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

Aktuell In-Memory (Prozess-Singleton `_ledger`, gleiches Muster wie die
Mock-Stores in tools/crm_integration.py) — Einträge gehen bei Neustart
verloren. TODO vor Produktivbetrieb mit echten Kunden: persistenter Store
(Postgres/Redis), identische Interface-Methoden.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

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


def _normalize(identifier: str) -> str:
    return identifier.strip().lower()


class ConsentLedger:
    """
    Auditierbares Opt-in/Opt-out-Register.

    `_history` ist Append-only (voller Audit-Trail, DSGVO Art. 30). `_latest`
    ist ein Index auf den jeweils neuesten Status pro (Identifier, Kanal) für
    schnelle is_allowed()-Abfragen vor jeder Outreach-Aktion.
    """

    def __init__(self) -> None:
        self._history: list[ConsentRecord] = []
        self._latest: dict[tuple[str, Channel], ConsentRecord] = {}

    def _record(
        self, identifier: str, channel: Channel, status: ConsentStatus, reason: str, recorded_by: str
    ) -> ConsentRecord:
        key_id = _normalize(identifier)
        entry = ConsentRecord(
            identifier=key_id, channel=channel, status=status, reason=reason, recorded_by=recorded_by
        )
        self._history.append(entry)
        self._latest[(key_id, channel)] = entry
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
        entry = self._latest.get((_normalize(identifier), channel))
        return entry.status if entry else None

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
        entries = self._history
        if identifier is not None:
            norm = _normalize(identifier)
            entries = [e for e in entries if e.identifier == norm]
        if channel is not None:
            entries = [e for e in entries if e.channel == channel]
        return list(entries)


# Prozessweiter Singleton — von allen Agenten geteilt (analog zu
# core.config.settings), damit ein über einen Kanal erfasster Opt-out auch
# für spätere Anfragen über andere Agenten sichtbar ist.
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
