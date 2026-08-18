"""
DLP (Data Loss Prevention) & PII Redaction layer.
Runs on every outbound payload before it leaves the agent boundary.
DSGVO Art. 25 – Privacy by Design: redaction happens in-process,
no raw PII ever reaches external APIs unless explicitly allowed.
"""
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from core.config import settings

logger = logging.getLogger(__name__)

# --- PII Pattern Registry -----------------------------------------------------------

_PII_PATTERNS: dict[str, re.Pattern] = {
    "email":      re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
    # phone_de: (?<![A-Za-z0-9\-]) prevents matching mid-UUID ("b96a-0532...") or mid-date ("2026-05-19").
    # KEIN "/" in der Wert-Klasse: das würde mit dem "/"-getrennten
    # Steuernummer-Format kollidieren (siehe tax_id) — ohne diese Einschränkung
    # matcht die führende "0"-Variante fälschlich Texte wie "06 418/9574".
    "phone_de":   re.compile(r"(?<![A-Za-z0-9\-])(\+49|0)\s*[\d\s\-]{6,15}(?!\d)"),
    # phone_at: gleiche Logik für österreichische Nummern (+43) — Novaras
    # gesamtes ICP ist Wien/Österreich, das fehlte bisher komplett und liess
    # AT-Telefonnummern unerkannt durchrutschen.
    "phone_at":   re.compile(r"(?<![A-Za-z0-9\-])(\+43)\s*[\d\s\-]{6,15}(?!\d)"),
    # IBAN: matches both compact (DE89370400440532013000) and spaced (DE89 3704 0044 0532 0130 00)
    "iban":       re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,8}(?:[ ]?[A-Z0-9]{0,4})?\b"),
    "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # German tax ID (Steuernummer) – 10-13 digits, sometimes with slashes
    "tax_id":     re.compile(r"\b\d{2,3}[/\s]?\d{3}[/\s]?\d{4,5}\b"),
}

# Kontaktdaten, die Agenten für ihre eigentliche Aufgabe benötigen (CRM-Eintrag,
# Welcome-Mail, Ticket-Zuordnung, Outreach, ...). Werden weiterhin ERKANNT
# (Findings/Audit-Trail, DSGVO Art. 30), aber NICHT redigiert — anders als
# echte Gefahrendaten (IBAN, Steuernummer, IP, Credentials), die immer ersetzt
# werden. check_and_redact() maskiert diese Treffer vor der Redaktion der
# übrigen Muster und stellt sie danach unverändert wieder her, damit z. B.
# tax_id nicht versehentlich einen Teil einer Telefonnummer mitredigiert.
_CONTACT_TYPES: frozenset[str] = frozenset({"email", "phone_de", "phone_at"})

# Prompt-Injection-Heuristik (portiert aus sdr_demo_referencia/app/dlp.py,
# gleicher Stil wie der bestehende Credential-Hard-Block: einfacher
# Substring-Abgleich auf mehrwortigen Phrasen).
_INJECTION_MARKERS: tuple[str, ...] = (
    "ignoriere die vorherigen anweisungen",
    "ignore previous instructions",
    "ignore all previous instructions",
    "du bist jetzt",
    "you are now",
    "system prompt",
    "systemanweisung",
    "reveal your instructions",
    "zeige deine anweisungen",
    "vergiss deine regeln",
    "act as",
    "verhalte dich als",
)

# Credential-Keywords, die auf ein Hard-Stop-Muster hindeuten. Ein reiner
# Substring-Treffer (z. B. "Passwort" in "...Passwort setzen.") ist KEIN Leak
# und darf nicht blockieren — daher verlangt _CREDENTIAL_PATTERN zusätzlich
# einen Delimiter + einen nachfolgenden Wert: "passwort:", "pwd=",
# "password ist xyz123". Die bloße Erwähnung des Wortes in normalem
# Fließtext (Business-Text, Checklisten, Anleitungen) triggert nicht mehr.
_CREDENTIAL_KEYWORDS: tuple[str, ...] = (
    "password", "passwort", "pwd", "geheimnis", "secret",
    "private_key", "access_token", "api_key",
)
_CREDENTIAL_DELIMITER = r"(?::\s*|=\s*|\s+ist\s+|\s+is\s+)"
_CREDENTIAL_PATTERN = re.compile(
    r"\b(?:" + "|".join(_CREDENTIAL_KEYWORDS) + r")\b" + _CREDENTIAL_DELIMITER + r"\S+",
    re.IGNORECASE,
)
# "Bearer <token>" (HTTP Authorization Header) — hier ist das Leerzeichen
# selbst der natürliche Delimiter, "bearer" ist kein Wort in normalem
# deutschen/englischen Fließtext, daher kein eigenes False-Positive-Risiko.
_BEARER_PATTERN = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)


@dataclass
class DLPResult:
    approved: bool
    redacted_text: str
    findings: list[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None


class SecurityLayer:
    """
    Stateless security utility used by BaseAgent before any outbound call.
    All methods are classmethods – no instantiation required.
    """

    @classmethod
    def check_and_redact(cls, text: str) -> DLPResult:
        """
        1. Hard-block check: refuse if obviously sensitive credentials are present.
        2. PII redaction: replace detected PII with typed placeholders.
        Returns a DLPResult the caller must inspect before proceeding.
        """
        # Hard-stop: eine tatsächliche Credential-Zuweisung im Payload ist nie
        # akzeptabel. Erfordert Keyword + Delimiter + Wert (siehe
        # _CREDENTIAL_PATTERN oben), NICHT die bloße Erwähnung des Wortes.
        match = _CREDENTIAL_PATTERN.search(text) or _BEARER_PATTERN.search(text)
        if match:
            logger.warning("DLP hard-block triggered", extra={"match": match.group(0)})
            return DLPResult(
                approved=False,
                redacted_text=text,
                blocked_reason=f"Blocked credential pattern detected: '{match.group(0)}'",
            )

        # Hard-stop: Prompt-Injection-Versuch.
        lower = text.lower()
        injection_marker = next((m for m in _INJECTION_MARKERS if m in lower), None)
        if injection_marker:
            logger.warning("DLP hard-block triggered", extra={"injection_marker": injection_marker})
            return DLPResult(
                approved=False,
                redacted_text=text,
                blocked_reason=f"Prompt injection marker detected: '{injection_marker}'",
            )

        if not settings.enable_pii_redaction:
            return DLPResult(approved=True, redacted_text=text)

        findings: list[str] = []

        # 1) Kontaktdaten (E-Mail/Telefon) im UNVERÄNDERTEN Text erkennen und
        # deren Positionen merken (nicht ersetzen) — verhindert, dass ein
        # nachfolgendes Sensible-Daten-Pattern (z. B. tax_id) versehentlich
        # einen Teil einer nicht zu redigierenden Telefonnummer mit-erfasst
        # (das genaue Bug-Szenario, das diesen Fix motiviert hat). Nach
        # Ursprungsposition sortiert, damit Schritt 3 sie in Textreihenfolge
        # wiederherstellen kann, unabhängig davon, welcher Kontakt-Typ zuerst
        # gefunden wurde.
        contact_spans: list[tuple[int, int, str]] = []
        for contact_type in _CONTACT_TYPES:
            type_matches = list(_PII_PATTERNS[contact_type].finditer(text))
            if type_matches:
                findings.append(f"{contact_type}: {len(type_matches)} occurrence(s) detected (nicht redigiert)")
            contact_spans.extend((m.start(), m.end(), m.group(0)) for m in type_matches)
        contact_spans.sort(key=lambda span: span[0])

        # 2) Diese Stellen mit \0 maskieren (gleiche Länge wie das Original,
        # \0 kann in keinem PII-Muster vorkommen), dann alles redigieren, was
        # NICHT Kontaktdaten ist.
        masked = text
        for start, end, _ in contact_spans:
            masked = masked[:start] + ("\0" * (end - start)) + masked[end:]

        redacted = masked
        for pii_type, pattern in _PII_PATTERNS.items():
            if pii_type in _CONTACT_TYPES:
                continue
            matches = pattern.findall(redacted)
            if matches:
                findings.append(f"{pii_type}: {len(matches)} occurrence(s) redacted")
                redacted = pattern.sub(f"[REDACTED:{pii_type.upper()}]", redacted)

        # 3) Maskierte Kontaktdaten wieder durch die Originalwerte ersetzen,
        # in derselben Reihenfolge, in der sie im Text vorkommen.
        if contact_spans:
            originals = iter(original for _, _, original in contact_spans)
            redacted = re.sub(r"\0+", lambda _m: next(originals), redacted)

        if findings:
            logger.info("PII redaction applied", extra={"findings": findings})

        return DLPResult(approved=True, redacted_text=redacted, findings=findings)

    @classmethod
    def sanitize_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Recursively redact PII from all string values in a dict."""
        result: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                dlp = cls.check_and_redact(value)
                result[key] = dlp.redacted_text
            elif isinstance(value, dict):
                result[key] = cls.sanitize_dict(value)
            elif isinstance(value, list):
                result[key] = [
                    cls.sanitize_dict(item) if isinstance(item, dict)
                    else (cls.check_and_redact(item).redacted_text if isinstance(item, str) else item)
                    for item in value
                ]
            else:
                result[key] = value
        return result
