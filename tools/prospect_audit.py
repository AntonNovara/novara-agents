"""
Prospect-Audit ("Quick Strike") -- deterministische Website-Prüfung eines
Elektriker-Betriebs, bevor der SDR den Erstkontakt schreibt.

Prinzip: KEIN LLM und KEIN externer Dienst (Apify/Firecrawl/PageSpeed). Die
Prüfungen sind reine HTML-/HTTP-Checks; Score und Bericht sind deterministisch
und damit reproduzierbar und kostenlos. Ein LLM darf den Bericht später höchstens
umformulieren, nie Zahlen erfinden.

Bewusst NICHT enthalten: Schätzungen wie "Sie verlieren X Anfragen/Monat" -- dafür
gibt es keine belastbare Datenbasis. Ob ein Betrieb tatsächlich zu langsam auf
Anfragen antwortet, lässt sich nur mit einer echten Testanfrage prüfen und wird im
Bericht ausdrücklich als "nicht geprüft" ausgewiesen.

Sicherheit (SSRF): Der Endpoint holt vom Nutzer gelieferte URLs ab. Daher nur
http/https, nur öffentliche IPs (auch nach jedem Redirect), Größen- und Zeitlimit.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import Column, DateTime, Float, Integer, String, Text

from core.db import Base, SessionLocal, engine

logger = logging.getLogger(__name__)

MAX_BYTES = 1_500_000
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 8.0
USER_AGENT = "NovaraAuditBot/1.0 (+https://novaraautomation.com)"


# ---------------------------------------------------------------------------
# Datenmodell
# ---------------------------------------------------------------------------

@dataclass
class Check:
    id: str
    label: str
    passed: bool
    weight: int
    hint: str  # Handlungsempfehlung (Deutsch), nur relevant wenn nicht bestanden


@dataclass
class AuditResult:
    audit_id: str
    url: str
    final_url: str
    company: str
    score: int
    load_seconds: float
    checks: list[Check] = field(default_factory=list)
    error: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# SSRF-sicheres Abrufen
# ---------------------------------------------------------------------------

class AuditFetchError(Exception):
    pass


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise AuditFetchError("Leere URL")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise AuditFetchError("Nur http/https erlaubt")
    if not parsed.hostname:
        raise AuditFetchError("Ungültige URL")
    if parsed.username or parsed.password:
        raise AuditFetchError("Zugangsdaten in der URL nicht erlaubt")
    return raw


def _assert_public_host(hostname: str) -> None:
    """Wirft AuditFetchError, wenn der Host auf eine nicht-öffentliche IP zeigt."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise AuditFetchError(f"Host nicht auflösbar: {hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise AuditFetchError("Host zeigt auf keine öffentliche IP-Adresse")


def fetch_page(url: str) -> tuple[str, str, float]:
    """Gibt (final_url, html, load_seconds) zurück. Prüft jeden Redirect erneut."""
    current = normalize_url(url)
    started = time.monotonic()
    with httpx.Client(follow_redirects=False, timeout=TIMEOUT_SECONDS,
                      headers={"User-Agent": USER_AGENT}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _assert_public_host(urlparse(current).hostname or "")
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise AuditFetchError("Redirect ohne Ziel")
                    current = normalize_url(urljoin(current, location))
                    continue
                if resp.status_code >= 400:
                    raise AuditFetchError(f"HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype.lower():
                    raise AuditFetchError("Antwort ist kein HTML")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        break
                html = bytes(body[:MAX_BYTES]).decode(resp.encoding or "utf-8", errors="replace")
                return current, html, round(time.monotonic() - started, 2)
    raise AuditFetchError("Zu viele Weiterleitungen")


# ---------------------------------------------------------------------------
# Prüfungen (rein deterministisch, HTML-basiert)
# ---------------------------------------------------------------------------

def _has(pattern: str, html: str) -> bool:
    return re.search(pattern, html, re.IGNORECASE | re.DOTALL) is not None


def run_checks(final_url: str, html: str, load_seconds: float) -> list[Check]:
    is_https = final_url.lower().startswith("https://")
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title_len = len(title.group(1).strip()) if title else 0
    has_form = _has(r"<form\b", html) and _has(r"<(textarea|input[^>]+type=[\"']?(email|tel))", html)

    return [
        Check("whatsapp", "WhatsApp-Kontakt auf der Website",
              _has(r"(wa\.me/|api\.whatsapp\.com|whatsapp://|href=[\"'][^\"']*whatsapp)", html), 15,
              "Kein WhatsApp-Button: Kunden müssen anrufen oder ein Formular ausfüllen -- viele Anfragen gehen so verloren."),
        Check("mobile", "Mobil-optimiert (Viewport)",
              _has(r"<meta[^>]+name=[\"']viewport[\"']", html), 15,
              "Keine mobile Darstellung erkennbar: Die meisten Handwerker-Suchen laufen auf dem Handy."),
        Check("tel", "Direkt anrufbare Telefonnummer",
              _has(r"href=[\"']tel:", html), 10,
              "Telefonnummer ist nicht antippbar (tel:-Link) -- am Handy ein unnötiger Umweg."),
        Check("form", "Kontaktformular vorhanden", has_form, 10,
              "Kein Kontaktformular: Anfragen außerhalb der Bürozeiten haben keinen Kanal."),
        Check("speed", "Schnelle Ladezeit (unter 2,5 s)", load_seconds < 2.5, 10,
              f"Seite lädt {load_seconds:.1f} s -- langsame Seiten verlieren Besucher, bevor sie anfragen."),
        Check("https", "Verschlüsselte Verbindung (HTTPS)", is_https, 10,
              "Keine HTTPS-Verbindung: Browser warnen Besucher, das schreckt Neukunden ab."),
        Check("title", "Aussagekräftiger Seitentitel", 10 <= title_len <= 70, 5,
              "Seitentitel fehlt oder ist zu kurz/lang -- schlechter für die Google-Suche."),
        Check("description", "Meta-Beschreibung für Google",
              _has(r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"'][^\"']{30,}", html), 5,
              "Keine Meta-Beschreibung: Google zeigt einen zufälligen Textausschnitt."),
        Check("impressum", "Impressum verlinkt",
              _has(r"impressum", html), 5,
              "Kein Impressum-Link gefunden -- in Österreich Pflicht (ECG/Mediengesetz)."),
        Check("schema", "Lokale Unternehmensdaten für Google (LocalBusiness/Electrician)",
              _has(r"\"@type\"\s*:\s*\"?\[?\"?(LocalBusiness|Electrician|HomeAndConstructionBusiness)", html), 5,
              "Keine strukturierten Firmendaten: Google kann Öffnungszeiten und Einsatzgebiet schlechter anzeigen."),
        Check("notdienst", "Notdienst / 24h erwähnt",
              _has(r"(notdienst|24\s*/?\s*7|24\s*h|24[- ]stunden|störungsdienst)", html), 5,
              "Kein Notdienst-Hinweis: Notfall-Kunden wählen den Betrieb, der Erreichbarkeit zeigt."),
        Check("maps", "Standort / Karte eingebunden",
              _has(r"(google\.[a-z.]+/maps|maps\.google|openstreetmap|goo\.gl/maps)", html), 5,
              "Keine Karte/Standortangabe -- Kunden wollen sehen, ob der Betrieb im Einsatzgebiet liegt."),
    ]


def compute_score(checks: list[Check]) -> int:
    total = sum(c.weight for c in checks)
    if not total:
        return 0
    return round(100 * sum(c.weight for c in checks if c.passed) / total)


# ---------------------------------------------------------------------------
# Bericht (deterministisch, Deutsch)
# ---------------------------------------------------------------------------

def render_report_de(result: AuditResult, top_n: int = 3) -> str:
    """Kurzer Bericht für die Erstansprache. Erfindet keine Zahlen."""
    name = result.company or "Ihr Betrieb"
    if result.error:
        return f"Audit für {name} nicht möglich: {result.error}"
    gaps = sorted((c for c in result.checks if not c.passed), key=lambda c: -c.weight)[:top_n]
    lines = [f"Website-Check für {name} ({result.final_url}): {result.score}/100 Punkte", ""]
    if gaps:
        lines.append("Größte Lücken bei der Anfragen-Erreichbarkeit:")
        for i, c in enumerate(gaps, 1):
            lines.append(f"{i}. {c.label} -- {c.hint}")
    else:
        lines.append("Keine grundlegenden Lücken gefunden.")
    lines += [
        "",
        "Nicht geprüft: Wie schnell Anfragen tatsächlich beantwortet werden "
        "(dafür wäre eine echte Testanfrage nötig).",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persistenz (gleiche DB wie die anderen Module: Postgres in Prod, SQLite lokal)
# ---------------------------------------------------------------------------

class _ProspectAuditRow(Base):
    __tablename__ = "prospect_audits"

    audit_id = Column(String(36), primary_key=True)
    url = Column(String(2048), nullable=False)
    company = Column(String(255), nullable=False, default="")
    score = Column(Integer, nullable=False, default=0)
    load_seconds = Column(Float, nullable=False, default=0.0)
    error = Column(Text, nullable=False, default="")
    payload = Column(Text, nullable=False)  # komplettes AuditResult als JSON
    created_at = Column(DateTime(timezone=True), nullable=False)


Base.metadata.create_all(engine, tables=[_ProspectAuditRow.__table__])


def run_audit(url: str, company: str = "", persist: bool = True) -> AuditResult:
    """Führt den Audit aus. Fehler werden als result.error zurückgegeben, nicht geworfen."""
    result = AuditResult(
        audit_id=str(uuid.uuid4()), url=url, final_url=url, company=company.strip()[:255],
        score=0, load_seconds=0.0, created_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        final_url, html, secs = fetch_page(url)
        result.final_url, result.load_seconds = final_url, secs
        result.checks = run_checks(final_url, html, secs)
        result.score = compute_score(result.checks)
    except AuditFetchError as exc:
        result.error = str(exc)
    except httpx.HTTPError as exc:
        result.error = f"Abruf fehlgeschlagen ({type(exc).__name__})"
    except Exception:
        logger.exception("Prospect-Audit: unerwarteter Fehler")
        result.error = "Interner Fehler beim Audit"
    if persist:
        _save(result)
    return result


def _save(result: AuditResult) -> None:
    with SessionLocal() as session:
        session.add(_ProspectAuditRow(
            audit_id=result.audit_id, url=result.url[:2048], company=result.company,
            score=result.score, load_seconds=result.load_seconds, error=result.error,
            payload=json.dumps(result.to_dict(), ensure_ascii=False),
            created_at=datetime.fromisoformat(result.created_at),
        ))
        session.commit()


def get_audit(audit_id: str) -> Optional[dict]:
    with SessionLocal() as session:
        row = session.get(_ProspectAuditRow, audit_id)
        return json.loads(row.payload) if row else None
