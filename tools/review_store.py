"""
Review Store -- Persistenz für die Bewertungs-Seite (Pain 3). Hier landet das
direkte, private Feedback, das Kunden über GET/POST /r/{client_id} an den
Inhaber senden (siehe main.py und den Abschnitt "Bewertungs-Seite" in
CLAUDE.md). Der öffentliche Google-Weg läuft komplett außerhalb dieses
Systems -- nichts davon wird hier gespeichert.

Gleiches Persistenz-Muster wie tools/quote_store.py: eigene Tabelle, eigenes
Modul, SessionLocal/Base/engine aus core/db.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base, SessionLocal, engine


class _ReviewRow(Base):
    __tablename__ = "reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    # Direktes Feedback an den Betrieb (main.py review_submit()). rating 0 =
    # keine Sterne angegeben (die Seite fragt seit 2026-10-05 keine ab).
    feedback_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kunde_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    kunde_kontakt: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Legacy-Spalte aus der Zeit vor 2026-10-05 (Sterne-Weiterleitung); wird
    # immer False geschrieben, bleibt wegen create_all ohne Migration.
    redirected_to_google: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Base.metadata.create_all(bind=engine, tables=[_ReviewRow.__table__])


def create_review(
    client_id: str,
    rating: int,
    feedback_text: str,
    kunde_name: str,
    kunde_kontakt: str,
    redirected_to_google: bool,
) -> dict[str, Any]:
    review_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        session.add(_ReviewRow(
            id=review_id,
            client_id=client_id,
            rating=rating,
            feedback_text=feedback_text,
            kunde_name=kunde_name,
            kunde_kontakt=kunde_kontakt,
            redirected_to_google=redirected_to_google,
            notified=False,
            created_at=now,
        ))
        session.commit()
    return {"id": review_id, "created_at": now.isoformat()}


def mark_notified(review_id: str) -> None:
    with SessionLocal() as session:
        row = session.get(_ReviewRow, review_id)
        if row is not None:
            row.notified = True
            session.commit()


def list_reviews(client_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Zum manuellen Prüfen (z. B. main.py GET /api/v1/tools/reviews/{client_id},
    API-Key-geschützt) -- keine Business-Logik hier, reines Lesen, neueste zuerst."""
    with SessionLocal() as session:
        rows = (
            session.query(_ReviewRow)
            .filter(_ReviewRow.client_id == client_id)
            .order_by(_ReviewRow.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "rating": r.rating,
                "feedback_text": r.feedback_text,
                "kunde_name": r.kunde_name,
                "kunde_kontakt": r.kunde_kontakt,
                "redirected_to_google": r.redirected_to_google,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
