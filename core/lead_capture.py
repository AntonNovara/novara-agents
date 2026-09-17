"""
Lead-Capture-Register — erfasst strukturiert Kontaktdaten, die ein anonymer
Besucher im Landing-Page-Chat (agents/sdr_agent.py, InboundChatGraph) oder im
Vapi-Telefonat (agents/voice_agent.py, über den Post-Call-Transkript-Pfad in
main.py) preisgibt (Name, Telefon, E-Mail, Firma).

Zweck: Sobald ein Besucher genug Kontaktdaten preisgibt, um ihn tatsächlich
erreichen zu können (mindestens E-Mail ODER Telefon), soll das Team SOFORT
benachrichtigt werden (tools/lead_notifier.py) statt erst beim nächsten
manuellen CRM-Check. core/customer_state.py hält bereits einen geteilten
Kundenzustand über die 5-Agenten-Journey, ist aber NICHT für "wurde bereits
benachrichtigt?"-Deduplizierung gedacht (kein Notification-Flag) und wird
nur bei QUALIFIZIERTEN Leads geschrieben (InboundChatGraph.finalize()s
Schwellenlogik) — ein Besucher kann aber schon VOR Erreichen der
ICP-Schwelle seine Kontaktdaten nennen ("Ruf mich an: 0664...", noch bevor
genug über die Firma bekannt ist). Dieses Register ist deshalb bewusst
UNABHÄNGIG von der ICP-Qualifizierung: jede erkannte Kontaktangabe zählt.

Aktuell In-Memory (Prozess-Singleton `_register`, gleiches Muster wie
core/consent.py und core/customer_state.py) — Einträge gehen bei Neustart
verloren. TODO vor Produktivbetrieb: persistenter Store (Postgres/Redis).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from core.security import SecurityLayer

logger = logging.getLogger(__name__)


class CapturedLead(BaseModel):
    """Ein einzelner erfasster Lead — ein Eintrag pro (Quelle, Session)."""

    session_id: str
    source: str  # "landing_chat" | "voice"
    name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    # Auszug der Nachricht, in der die Daten auftauchten — Kontext für die
    # Notification-Mail (tools/lead_notifier.py), auf 500 Zeichen begrenzt,
    # damit ein sehr langes Voice-Transkript die Mail nicht sprengt.
    message_excerpt: str = ""
    captured_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notified: bool = False  # verhindert Mehrfach-Mails für dieselbe Session


class LeadCaptureRegister:
    """
    Erkennt und speichert Kontaktdaten aus Freitext, dedupliziert Benachrichtigungen.

    Schlüssel ist (source, session_id) statt E-Mail/Telefon — anders als
    core.consent (dort ist der Kontakt-Identifier selbst der Schlüssel, weil
    Opt-outs kontaktzentriert sind) geht es hier um "wurde für DIESE
    Konversation schon einmal benachrichtigt?", nicht um einen globalen
    Kontakt-Datensatz. Mehrere Sessions derselben Person erzeugen daher
    bewusst mehrere Einträge — core/customer_state.py übernimmt bereits die
    identifier-basierte Zusammenführung über die Journey hinweg, dieses
    Register ist reine Capture+Notify-Buchhaltung pro Konversation.
    """

    def __init__(self) -> None:
        self._leads: dict[tuple[str, str], CapturedLead] = {}

    def extract_contact_fields(
        self, message: str, visitor_info: Optional[dict[str, str]] = None
    ) -> dict[str, str]:
        """
        Bestes-Aufwand-Extraktion von E-Mail/Telefon aus Freitext
        (deterministisch, dieselben Regex-Muster wie core/security.py —
        Kontaktdaten werden dort nie redigiert, nur erkannt), ergänzt um
        bereits bekannte Formulardaten (visitor_info aus dem Chat-Widget,
        siehe main.py LandingVisitorInfo). Name/Firma kommen NICHT aus Regex
        (kein zuverlässiges Muster für freien Text) — die aufrufende Stelle
        kann sie zusätzlich LLM-extrahiert mitgeben (siehe
        InboundChatGraph.finalize()); diese Methode liefert nur, was Regex +
        Formulardaten sicher beisteuern können.
        """
        visitor_info = visitor_info or {}
        email = visitor_info.get("email") or SecurityLayer.extract_email(message) or ""
        phone = visitor_info.get("phone") or SecurityLayer.extract_phone(message) or ""
        return {
            "email": email,
            "phone": phone,
            "name": visitor_info.get("name", ""),
            "company": visitor_info.get("company", ""),
        }

    def capture(
        self,
        source: str,
        session_id: str,
        message: str,
        name: str = "",
        email: str = "",
        phone: str = "",
        company: str = "",
    ) -> Optional[CapturedLead]:
        """
        Speichert/aktualisiert den Lead für (source, session_id), falls
        mindestens E-Mail ODER Telefon bekannt ist — ein Name/eine Firma
        allein reicht nicht aus, um den Besucher tatsächlich zu erreichen.

        Gibt den CapturedLead zurück, wenn der Aufrufer jetzt eine
        Benachrichtigung auslösen soll (Erstfassung dieser Session, ODER
        eine frühere Benachrichtigung ist nachweislich noch nicht
        durchgelaufen — siehe mark_notified()) — sonst None.
        """
        if not email and not phone:
            return None

        key = (source, session_id)
        existing = self._leads.get(key)
        if existing is not None:
            # Additiv mergen, nie einen bekannten Wert mit leer überschreiben
            # — gleiche Philosophie wie core/customer_state.py update_stage().
            existing.name = name or existing.name
            existing.email = email or existing.email
            existing.phone = phone or existing.phone
            existing.company = company or existing.company
            if message:
                existing.message_excerpt = message[:500]
            self._leads[key] = existing
            return None if existing.notified else existing

        lead = CapturedLead(
            session_id=session_id,
            source=source,
            name=name,
            email=email,
            phone=phone,
            company=company,
            message_excerpt=message[:500],
        )
        self._leads[key] = lead
        logger.info("Lead captured", extra={"source": source, "session": session_id})
        return lead

    def mark_notified(self, source: str, session_id: str) -> None:
        lead = self._leads.get((source, session_id))
        if lead is not None:
            lead.notified = True

    def get(self, source: str, session_id: str) -> Optional[CapturedLead]:
        return self._leads.get((source, session_id))

    def all(self) -> list[CapturedLead]:
        return list(self._leads.values())


# Prozessweiter Singleton — gleiches Muster wie core/consent.py._ledger.
_register = LeadCaptureRegister()


def extract_contact_fields(message: str, visitor_info: Optional[dict[str, str]] = None) -> dict[str, str]:
    return _register.extract_contact_fields(message, visitor_info)


def capture(
    source: str,
    session_id: str,
    message: str,
    name: str = "",
    email: str = "",
    phone: str = "",
    company: str = "",
) -> Optional[CapturedLead]:
    return _register.capture(source, session_id, message, name=name, email=email, phone=phone, company=company)


def mark_notified(source: str, session_id: str) -> None:
    _register.mark_notified(source, session_id)


def get(source: str, session_id: str) -> Optional[CapturedLead]:
    return _register.get(source, session_id)


def all_leads() -> list[CapturedLead]:
    return _register.all()
