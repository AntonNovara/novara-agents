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
    # IBAN: matches both compact (DE89370400440532013000) and spaced (DE89 3704 0044 0532 0130 00)
    "iban":       re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,8}(?:[ ]?[A-Z0-9]{0,4})?\b"),
    # phone_de: (?<![A-Za-z0-9\-]) prevents matching mid-UUID ("b96a-0532...") or mid-date ("2026-05-19")
    "phone_de":   re.compile(r"(?<![A-Za-z0-9\-])(\+49|0)\s*[\d\s\-/]{6,15}(?!\d)"),
    "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # German tax ID (Steuernummer) – 10-13 digits, sometimes with slashes
    "tax_id":     re.compile(r"\b\d{2,3}[/\s]?\d{3}[/\s]?\d{4,5}\b"),
}

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

        if not settings.enable_pii_redaction:
            return DLPResult(approved=True, redacted_text=text)

        redacted = text
        findings: list[str] = []

        for pii_type, pattern in _PII_PATTERNS.items():
            matches = pattern.findall(redacted)
            if matches:
                findings.append(f"{pii_type}: {len(matches)} occurrence(s) redacted")
                redacted = pattern.sub(f"[REDACTED:{pii_type.upper()}]", redacted)

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
