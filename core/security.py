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
    # (?:[\s\-]?\d){5,14} statt einer freien [\d\s\-]{6,15}-Zeichenklasse: jede
    # Wiederholung verlangt eine Ziffer, daher endet der Treffer immer auf einer
    # Ziffer und kann kein nachgestelltes " - " (Satz-Trenner, keine
    # Telefonnummer) mehr verschlucken, z. B. in "... 040 1234567 - Mail1 ...".
    "phone_de":   re.compile(r"(?<![A-Za-z0-9\-])(\+49|0)(?:[\s\-]?\d){5,14}(?!\d)"),
    # phone_at: gleiche Logik für österreichische Nummern (+43) — Novaras
    # gesamtes ICP ist Wien/Österreich, das fehlte bisher komplett und liess
    # AT-Telefonnummern unerkannt durchrutschen.
    "phone_at":   re.compile(r"(?<![A-Za-z0-9\-])(\+43)(?:[\s\-]?\d){5,14}(?!\d)"),
    # IBAN: matches both compact (DE89370400440532013000) and spaced (DE89 3704 0044 0532 0130 00)
    "iban":       re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,8}(?:[ ]?[A-Z0-9]{0,4})?\b"),
    "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # Österreichische/deutsche Steuernummer. Belegte reale Formate im System:
    # "06 418/9574" (Finanzamt-Schreiben, novara-admin/steuern/) und die
    # Rechnungsvorlage novara-admin/vorlagen/Rechnung_Vorlage.md verwendet
    # "XX-XXX/XXXX" — Trennzeichen ist also nicht immer "/", kann pro Stelle
    # Leerzeichen, "/" ODER "-" sein. Trennzeichen ist hier bewusst PFLICHT
    # (kein "?"), analog zur unabhängigen Referenz-Implementierung in
    # la-maquina-de-confianza/utils/sanitizer.py — eine reine 9-10-stellige
    # Ziffernfolge ohne jedes Trennzeichen wird von KEINER der beiden
    # Implementierungen als Steuernummer behandelt.
    "tax_id":     re.compile(r"\b\d{2,3}[\s/\-]\d{3}[\s/\-]\d{4,5}\b"),
}

# Kontaktdaten, die Agenten für ihre eigentliche Aufgabe benötigen (CRM-Eintrag,
# Welcome-Mail, Ticket-Zuordnung, Outreach, ...). Werden weiterhin ERKANNT
# (Findings/Audit-Trail, DSGVO Art. 30), aber NICHT redigiert — anders als
# echte Gefahrendaten (IBAN, Steuernummer, IP, Credentials), die immer ersetzt
# werden.
#
# check_and_redact() ermittelt Sensible-Daten-Treffer (iban/ip_address/tax_id)
# ZUERST auf dem unveränderten Text und lässt sie bei Überlappung IMMER
# gewinnen — ein Kontakt-Kandidat, der sich mit einer Steuernummer & Co.
# überschneidet, wird verworfen statt geschützt. Das ist strukturell robuster
# als einzelne Zeichen aus den Kontakt-Mustern auszuschließen (der vorherige
# Ansatz: "/" aus phone_de entfernen, brach bereits am nächsten Trennzeichen
# wieder) und bleibt auch dann sicher, wenn künftig weitere Muster in eine der
# beiden Kategorien dazukommen.
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

        # 1) Sensible-Daten-Treffer (alles außer _CONTACT_TYPES) ZUERST auf dem
        # unveränderten Text bestimmen. Sie haben Vorrang vor Kontaktdaten: ein
        # Kandidat, der sich mit einem Sensible-Daten-Treffer überschneidet
        # (z. B. eine Steuernummer, die auch phone_de's Zeichenklasse erfüllt),
        # wird NICHT als Kontakt behandelt und bleibt redigierbar.
        sensitive_spans: list[tuple[int, int]] = []
        for pii_type, pattern in _PII_PATTERNS.items():
            if pii_type in _CONTACT_TYPES:
                continue
            sensitive_spans.extend((m.start(), m.end()) for m in pattern.finditer(text))

        def _overlaps_sensitive(start: int, end: int) -> bool:
            return any(s_start < end and start < s_end for s_start, s_end in sensitive_spans)

        # 2) Kontaktdaten (E-Mail/Telefon) im unveränderten Text erkennen und
        # Kandidaten verwerfen, die mit einem Sensible-Daten-Treffer
        # überlappen (siehe oben). phone_de und phone_at können sich
        # GEGENSEITIG überlappen (phone_de's "0"-Zweig matcht z. B. auch
        # innerhalb einer bereits als phone_at erkannten Nummer, "+43 040
        # ..." → "0" + "40 ..."), daher zusätzlich: bei Überlappung zwischen
        # zwei Kontakt-Kandidaten gewinnt der LÄNGERE Treffer, kürzere
        # überlappende Kandidaten werden verworfen. Das ist nötig, damit der
        # anschließende Maskierungsschritt garantiert nicht überlappende
        # Spans erhält (sonst würde die spätere, ineinander verschachtelte
        # Maskierung die zuerst gesetzten Marker teilweise wieder zerstören).
        candidates: list[tuple[int, int, str, str]] = [
            (m.start(), m.end(), m.group(0), contact_type)
            for contact_type in _CONTACT_TYPES
            for m in _PII_PATTERNS[contact_type].finditer(text)
            if not _overlaps_sensitive(m.start(), m.end())
        ]
        candidates.sort(key=lambda c: c[1] - c[0], reverse=True)  # längste zuerst

        accepted: list[tuple[int, int, str, str]] = []
        for start, end, matched_text, ctype in candidates:
            if not any(a_start < end and start < a_end for a_start, a_end, _, _ in accepted):
                accepted.append((start, end, matched_text, ctype))

        type_counts: dict[str, int] = {}
        for _, _, _, ctype in accepted:
            type_counts[ctype] = type_counts.get(ctype, 0) + 1
        for ctype, count in type_counts.items():
            findings.append(f"{ctype}: {count} occurrence(s) detected (nicht redigiert)")

        contact_spans: list[tuple[int, int, str]] = sorted(
            ((start, end, matched_text) for start, end, matched_text, _ in accepted),
            key=lambda span: span[0],
        )

        masked = text
        for i in range(len(contact_spans) - 1, -1, -1):
            start, end, _ = contact_spans[i]
            masked = masked[:start] + f"\x00{i}\x00" + masked[end:]

        # 3) Alles redigieren, was NICHT Kontaktdaten ist.
        redacted = masked
        for pii_type, pattern in _PII_PATTERNS.items():
            if pii_type in _CONTACT_TYPES:
                continue
            matches = pattern.findall(redacted)
            if matches:
                findings.append(f"{pii_type}: {len(matches)} occurrence(s) redacted")
                redacted = pattern.sub(f"[REDACTED:{pii_type.upper()}]", redacted)

        # 4) Maskierte Kontaktdaten anhand ihres Index wieder durch die
        # Originalwerte ersetzen. "\x00" (NUL) kommt in keinem PII-Muster vor,
        # daher können die Marker nicht mit echtem Text oder untereinander
        # verschmelzen — Zuordnung erfolgt über den eingebetteten Index, nicht
        # über Reihenfolge/Zählung.
        if contact_spans:
            redacted = re.sub(
                r"\x00(\d+)\x00",
                lambda m: contact_spans[int(m.group(1))][2],
                redacted,
            )

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
