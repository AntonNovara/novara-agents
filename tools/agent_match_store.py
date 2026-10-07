"""
Match Store -- Persistenz für Novara Agents (B2B-Matchmaking zwischen
"digitalen Zwillingen"). Zwei Tabellen: die Profile aus dem Onboarding
(`match_profiles`) und die Ergebnisse der Agent-zu-Agent-Simulation
(`agent_matches`, siehe agents/agent_matcher.py).

Gleiches Muster wie tools/review_store.py / tools/quote_store.py: eigene
Tabellen, eigenes Modul, SessionLocal/Base/engine aus core/db.py, kein Alembic
(rein additiv -- create_all legt fehlende Tabellen an).

Zugriff durch den Profil-Inhaber läuft über ein Zugriffs-Token, das beim
Onboarding einmalig im Klartext zurückgegeben wird; gespeichert wird nur der
SHA-256-Hash. Kontaktdaten (E-Mail) verlassen dieses Modul NIE über
`public_view()` -- weder an andere Profile noch ins LLM.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base, SessionLocal, engine

ROLE_TYPES = ("partner", "cliente", "inversor")


class _ProfileRow(Base):
    __tablename__ = "match_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    empresa: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role_type: Mapped[str] = mapped_column(String(16), nullable=False)
    ofrece: Mapped[str] = mapped_column(Text, nullable=False)
    busca: Mapped[str] = mapped_column(Text, nullable=False)
    innegociables: Mapped[str] = mapped_column(Text, nullable=False, default="")
    consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class _MatchRow(Base):
    __tablename__ = "agent_matches"
    __table_args__ = (UniqueConstraint("profile_a_id", "profile_b_id", name="uq_agent_matches_pair"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Immer sortiert (a < b), damit (A,B) und (B,A) dasselbe Paar sind.
    profile_a_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    profile_b_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    compatibility_pct: Mapped[int] = mapped_column(Integer, nullable=False)
    agreement_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    transcript_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    deal_breaker_violated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Base.metadata.create_all(bind=engine, tables=[_ProfileRow.__table__, _MatchRow.__table__])


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _profile_dict(r: _ProfileRow) -> dict[str, Any]:
    return {
        "id": r.id, "nombre": r.nombre, "empresa": r.empresa, "email": r.email,
        "role_type": r.role_type, "ofrece": r.ofrece, "busca": r.busca,
        "innegociables": r.innegociables, "created_at": r.created_at.isoformat(),
    }


def public_view(profile: dict[str, Any]) -> dict[str, Any]:
    """Was ein ANDERES Profil von diesem sehen darf: kein Name, keine E-Mail,
    keine Dealbreaker."""
    return {
        "empresa": profile.get("empresa") or "Empresa sin nombre",
        "role_type": profile["role_type"],
        "ofrece": profile["ofrece"],
        "busca": profile["busca"],
    }


def create_profile(
    nombre: str, empresa: str, email: str, role_type: str,
    ofrece: str, busca: str, innegociables: str, consent: bool,
) -> dict[str, Any]:
    """Legt ein Profil an. Gibt das Profil PLUS das Klartext-Token zurück
    (einzige Stelle, an der es existiert)."""
    if role_type not in ROLE_TYPES:
        raise ValueError(f"role_type muss einer von {ROLE_TYPES} sein")
    if not consent:
        raise ValueError("Ohne Einwilligung kann kein Profil gespeichert werden")
    profile_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(24)
    with SessionLocal() as session:
        row = _ProfileRow(
            id=profile_id, token_hash=_hash_token(token), nombre=nombre, empresa=empresa,
            email=email, role_type=role_type, ofrece=ofrece, busca=busca,
            innegociables=innegociables, consent=True, created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        out = _profile_dict(row)
    out["token"] = token
    return out


def get_profile(profile_id: str) -> Optional[dict[str, Any]]:
    with SessionLocal() as session:
        row = session.get(_ProfileRow, profile_id)
        return _profile_dict(row) if row else None


def verify_token(profile_id: str, token: str) -> bool:
    if not token:
        return False
    with SessionLocal() as session:
        row = session.get(_ProfileRow, profile_id)
        return bool(row) and hmac.compare_digest(row.token_hash, _hash_token(token))


def list_profiles() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.query(_ProfileRow).order_by(_ProfileRow.created_at.asc()).all()
        return [_profile_dict(r) for r in rows]


def _ordered(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def pair_exists(a_id: str, b_id: str) -> bool:
    lo, hi = _ordered(a_id, b_id)
    with SessionLocal() as session:
        return session.query(_MatchRow.id).filter_by(profile_a_id=lo, profile_b_id=hi).first() is not None


def save_match(
    a_id: str, b_id: str, compatibility_pct: int, agreement_summary: str,
    transcript: list[dict[str, str]], deal_breaker_violated: bool,
) -> str:
    lo, hi = _ordered(a_id, b_id)
    match_id = str(uuid.uuid4())
    with SessionLocal() as session:
        session.add(_MatchRow(
            id=match_id, profile_a_id=lo, profile_b_id=hi, compatibility_pct=compatibility_pct,
            agreement_summary=agreement_summary, transcript_json=json.dumps(transcript, ensure_ascii=False),
            deal_breaker_violated=deal_breaker_violated, created_at=datetime.now(timezone.utc),
        ))
        session.commit()
    return match_id


def _match_dict(r: _MatchRow) -> dict[str, Any]:
    return {
        "id": r.id, "profile_a_id": r.profile_a_id, "profile_b_id": r.profile_b_id,
        "compatibility_pct": r.compatibility_pct, "agreement_summary": r.agreement_summary,
        "transcript": json.loads(r.transcript_json), "deal_breaker_violated": r.deal_breaker_violated,
        "created_at": r.created_at.isoformat(),
    }


def list_matches_for_profile(profile_id: str, min_pct: int = 0) -> list[dict[str, Any]]:
    """Alle Matches eines Profils, bester zuerst; `other_profile_id` zeigt auf
    die Gegenseite. Enthält das Transkript -- für Nutzer-Antworten muss der
    Aufrufer es entfernen (siehe main.py matching_dashboard)."""
    with SessionLocal() as session:
        rows = (
            session.query(_MatchRow)
            .filter((_MatchRow.profile_a_id == profile_id) | (_MatchRow.profile_b_id == profile_id))
            .filter(_MatchRow.compatibility_pct >= min_pct)
            .order_by(_MatchRow.compatibility_pct.desc())
            .all()
        )
        out = []
        for r in rows:
            d = _match_dict(r)
            d["other_profile_id"] = r.profile_b_id if r.profile_a_id == profile_id else r.profile_a_id
            out.append(d)
        return out
