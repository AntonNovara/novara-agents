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

Persistiert über core/db.py (Postgres in Produktion, SQLite lokal) seit
21.09.2026 — vorher In-Memory-Prozess-Singleton (analog zu core/consent.py
und core/customer_state.py), Einträge gingen bei jedem Neustart verloren.
Alle öffentlichen Methoden (extract_contact_fields, capture, mark_notified,
get, all) sind unverändert.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base, SessionLocal, engine
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


class _CapturedLeadRow(Base):
    __tablename__ = "captured_leads"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    company: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    message_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# Nur diese Tabelle (siehe core/consent.py für die Begründung).
Base.metadata.create_all(bind=engine, tables=[_CapturedLeadRow.__table__])


def _row_to_lead(row: _CapturedLeadRow) -> CapturedLead:
    return CapturedLead(
        session_id=row.session_id,
        source=row.source,
        name=row.name,
        email=row.email,
        phone=row.phone,
        company=row.company,
        message_excerpt=row.message_excerpt,
        captured_at=row.captured_at.isoformat(),
        notified=row.notified,
    )


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

        with SessionLocal() as session:
            row = session.get(_CapturedLeadRow, (source, session_id))
            if row is not None:
                # Additiv mergen, nie einen bekannten Wert mit leer
                # überschreiben — gleiche Philosophie wie
                # core/customer_state.py update_stage().
                row.name = name or row.name
                row.email = email or row.email
                row.phone = phone or row.phone
                row.company = company or row.company
                if message:
                    row.message_excerpt = message[:500]
                already_notified = row.notified
                session.commit()
                return None if already_notified else _row_to_lead(row)

            row = _CapturedLeadRow(
                source=source,
                session_id=session_id,
                name=name,
                email=email,
                phone=phone,
                company=company,
                message_excerpt=message[:500],
                captured_at=datetime.now(timezone.utc),
                notified=False,
            )
            session.add(row)
            session.commit()
            lead = _row_to_lead(row)

        logger.info("Lead captured", extra={"source": source, "session": session_id})
        return lead

    def mark_notified(self, source: str, session_id: str) -> None:
        with SessionLocal() as session:
            row = session.get(_CapturedLeadRow, (source, session_id))
            if row is not None:
                row.notified = True
                session.commit()

    def get(self, source: str, session_id: str) -> Optional[CapturedLead]:
        with SessionLocal() as session:
            row = session.get(_CapturedLeadRow, (source, session_id))
            return _row_to_lead(row) if row else None

    def all(self) -> list[CapturedLead]:
        with SessionLocal() as session:
            rows = session.query(_CapturedLeadRow).all()
            return [_row_to_lead(r) for r in rows]


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
