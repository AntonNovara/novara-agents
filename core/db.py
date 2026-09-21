"""
Persistenter Store — ersetzt die vier bislang In-Memory-Prozess-Singletons
(core/consent.py, core/customer_state.py, core/lead_capture.py,
tools/sequence_scheduler.py), die bei jedem Neustart/Redeploy ihren Inhalt
verloren (siehe CLAUDE.md, "Bekannte Einschränkungen" -- alle vier trugen
denselben TODO-Kommentar: "persistenter Store (Postgres/Redis) vor
Produktivbetrieb").

Eine gemeinsame `SessionLocal` für alle vier Module, analog zum bestehenden
Singleton-Muster (core.llm.build_llm(), core.config.settings) -- kein
Connection-Pool pro Modul. `pool_pre_ping=True`, weil Railways verwaltetes
Postgres Verbindungen nach Inaktivität serverseitig trennen kann; ohne das
würde die ERSTE Anfrage nach einer längeren Ruhephase mit einer toten
Connection fehlschlagen statt automatisch neu zu verbinden.

Ohne DATABASE_URL (lokale Entwicklung, `test_system.py`, CI) fällt dies auf
eine lokale SQLite-Datei zurück -- kein zusätzliches lokales Postgres nötig,
um die vier Module zu importieren/zu testen. Das Schema ist bewusst simpel
gehalten (keine Postgres-spezifischen Typen wie JSONB), damit derselbe Code
unverändert gegen beide Dialekte läuft.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from core.config import settings

_DEFAULT_SQLITE_URL = "sqlite:///./novara_dev.db"


def _resolve_database_url() -> str:
    url = settings.database_url.strip()
    if not url:
        return _DEFAULT_SQLITE_URL
    # Railway (wie die meisten verwalteten Postgres-Anbieter) liefert
    # DATABASE_URL mit dem Schema "postgres://" -- SQLAlchemy 2.x erwartet
    # "postgresql://" und braucht zusätzlich den Treiber-Suffix, wenn nicht
    # der Default-Treiber (psycopg2, hier explizit) gemeint ist.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg2" not in url:
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


_DATABASE_URL = _resolve_database_url()
_connect_args = {"check_same_thread": False} if _DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(_DATABASE_URL, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """
    Legt alle Tabellen an, falls sie noch nicht existieren (`CREATE TABLE IF
    NOT EXISTS`, via `checkfirst=True` -- SQLAlchemys Default). Bewusst kein
    Alembic für diese erste Runde: additive, kleine Schemata ohne bisherige
    Produktivdaten, die migriert werden müssten -- passt zur "Mock durch
    swap-in-fähige Prod-Implementierung ersetzen"-Pragmatik, die der Rest
    dieses Repos bereits durchgängig verfolgt (siehe CLAUDE.md). Aufgerufen
    aus main.py's lifespan() beim Start, NACH dem Import der vier Module
    unten (deren Model-Klassen müssen an `Base.metadata` registriert sein,
    bevor `create_all()` läuft) -- die Imports hier stellen das sicher, auch
    wenn main.py sie aus anderen Gründen längst importiert hat.
    """
    from core import consent as _consent  # noqa: F401
    from core import customer_state as _customer_state  # noqa: F401
    from core import lead_capture as _lead_capture  # noqa: F401
    from tools import sequence_scheduler as _sequence_scheduler  # noqa: F401

    Base.metadata.create_all(bind=engine)
