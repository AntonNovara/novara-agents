"""
Quote Store -- Persistenz für den Angebots-Generator (agents/quote_agent.py).

Ein Angebot durchläuft genau diese Status-Kette:
  draft -> pending_approval -> approved -> sent
                            (oder: pending_approval -> rejected)

`draft` existiert nur transient im Agenten-Graphen (agents/quote_agent.py
persistiert erst NACH dem PDF-Export als "pending_approval") -- das
Elektriker-Freigabe-Gate (siehe main.py POST /api/v1/quotes/{id}/approve)
ist hart: kein Datensatz verlässt "pending_approval" ohne einen gültigen
approval_token, und der Kunde bekommt das PDF erst im Status "sent".

Gleiches Persistenz-Muster wie tools/telnyx_voice.py (_MissedCallRow):
eigene Tabelle, eigenes Modul, SessionLocal/Base/engine aus core/db.py.
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base, SessionLocal, engine


class _QuoteRow(Base):
    __tablename__ = "quotes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kunde_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    kunde_kontakt: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # JSON-Blob statt eigener Positions-Tabelle -- analog zu
    # tools/sequence_scheduler.py (eine Sequenz = ein JSON-Blob pro Zeile):
    # eine Angebotsposition wird nie einzeln gelesen/geschrieben, immer nur
    # als vollständige, geordnete Liste.
    positionen_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    gesamtsumme_eur: Mapped[Optional[float]] = mapped_column(nullable=True)
    hat_offene_positionen: Mapped[bool] = mapped_column(nullable=False, default=False)
    pdf_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_approval")
    approval_token: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="whatsapp")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Base.metadata.create_all(bind=engine, tables=[_QuoteRow.__table__])

_STATUS_ORDER = ["pending_approval", "approved", "sent"]


def create_quote(
    client_id: str,
    kunde_name: str,
    kunde_kontakt: str,
    positionen: list[dict[str, Any]],
    gesamtsumme_eur: Optional[float],
    hat_offene_positionen: bool,
    pdf_path: str,
    source: str = "whatsapp",
) -> dict[str, Any]:
    """Legt ein neues Angebot im Status "pending_approval" an und erzeugt
    einen zufälligen, nicht-erratbaren Freigabe-Token (secrets.token_urlsafe --
    gleiche Bibliothek wie beim VAPI_SERVER_SECRET, kryptografisch sicher)."""
    quote_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        session.add(_QuoteRow(
            id=quote_id,
            client_id=client_id,
            kunde_name=kunde_name,
            kunde_kontakt=kunde_kontakt,
            positionen_json=json.dumps(positionen, ensure_ascii=False),
            gesamtsumme_eur=gesamtsumme_eur,
            hat_offene_positionen=hat_offene_positionen,
            pdf_path=pdf_path,
            status="pending_approval",
            approval_token=token,
            source=source,
            created_at=now,
            updated_at=now,
        ))
        session.commit()
    return {"id": quote_id, "approval_token": token, "status": "pending_approval"}


def _to_dict(row: _QuoteRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "client_id": row.client_id,
        "kunde_name": row.kunde_name,
        "kunde_kontakt": row.kunde_kontakt,
        "positionen": json.loads(row.positionen_json),
        "gesamtsumme_eur": row.gesamtsumme_eur,
        "hat_offene_positionen": row.hat_offene_positionen,
        "pdf_path": row.pdf_path,
        "status": row.status,
        "source": row.source,
        "created_at": row.created_at.isoformat(),
    }


def get_quote(quote_id: str) -> Optional[dict[str, Any]]:
    with SessionLocal() as session:
        row = session.get(_QuoteRow, quote_id)
        return _to_dict(row) if row is not None else None


def approve(quote_id: str, token: str) -> tuple[bool, str]:
    """Setzt ein Angebot von "pending_approval" auf "approved" -- NUR bei
    exaktem Token-Match (secrets.compare_digest gegen Timing-Angriffe,
    gleiches Prinzip wie main.py::_verify_vapi_secret). Gibt (ok, reason)
    zurück statt zu werfen -- main.py entscheidet über den HTTP-Statuscode."""
    with SessionLocal() as session:
        row = session.get(_QuoteRow, quote_id)
        if row is None:
            return False, "not_found"
        if not secrets.compare_digest(row.approval_token, token or ""):
            return False, "invalid_token"
        if row.status != "pending_approval":
            return False, f"already_{row.status}"
        row.status = "approved"
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
    return True, "approved"


def reject(quote_id: str, token: str) -> tuple[bool, str]:
    with SessionLocal() as session:
        row = session.get(_QuoteRow, quote_id)
        if row is None:
            return False, "not_found"
        if not secrets.compare_digest(row.approval_token, token or ""):
            return False, "invalid_token"
        if row.status != "pending_approval":
            return False, f"already_{row.status}"
        row.status = "rejected"
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
    return True, "rejected"


def mark_sent(quote_id: str) -> None:
    """Setzt "approved" -> "sent", NUR nachdem das PDF wirklich beim Kunden
    zugestellt wurde (main.py ruft das erst nach erfolgreichem
    whatsapp_cloud.send_document())."""
    with SessionLocal() as session:
        row = session.get(_QuoteRow, quote_id)
        if row is not None and row.status == "approved":
            row.status = "sent"
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
