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
    # Anthropic API-Keys (Präfix "sk-ant-"). Ergänzt das bestehende
    # Keyword+Delimiter-Hard-Block-Muster (_CREDENTIAL_PATTERN, z. B.
    # "api_key: sk-ant-...") um den Fall, dass der Key OHNE erkennbares
    # Schlüsselwort/Delimiter im Text auftaucht (z. B. einfach eingefügt in
    # einen Satz) — dort greift der Hard-Block nicht, dieses Muster schon.
    # Redact-statt-Hard-Block ist hier bewusst konsistent mit der
    # unabhängigen Referenz-Implementierung la-maquina-de-confianza/
    # utils/sanitizer.py (gleiches Regex-Muster, dortiger Typname
    # "anthropic_key"), nicht mit dem strikteren Hard-Block-Pfad.
    "anthropic_api_key": re.compile(r"sk-ant-[a-zA-Z0-9\-_]{20,}", re.IGNORECASE),
}

# Kontaktdaten, die Agenten für ihre eigentliche Aufgabe benötigen (CRM-Eintrag,
# Welcome-Mail, Ticket-Zuordnung, Outreach, ...). Werden weiterhin ERKANNT
# (Findings/Audit-Trail, DSGVO Art. 30), aber NICHT redigiert — anders als
# echte Gefahrendaten (IBAN, Steuernummer, IP, Credentials), die immer ersetzt
# werden.
#
# check_and_redact() ermittelt Sensible-Daten-Treffer (iban/ip_address/tax_id/
# anthropic_api_key — alles außer _CONTACT_TYPES) ZUERST auf dem unveränderten
# Text und lässt sie bei Überlappung IMMER
# gewinnen — ein Kontakt-Kandidat, der sich mit einer Steuernummer & Co.
# überschneidet, wird verworfen statt geschützt. Das ist strukturell robuster
# als einzelne Zeichen aus den Kontakt-Mustern auszuschließen (der vorherige
# Ansatz: "/" aus phone_de entfernen, brach bereits am nächsten Trennzeichen
# wieder) und bleibt auch dann sicher, wenn künftig weitere Muster in eine der
# beiden Kategorien dazukommen.
_CONTACT_TYPES: frozenset[str] = frozenset({"email", "phone_de", "phone_at"})

# Prompt-Injection-Heuristik, zweistufig (Nachfolger der ursprünglichen,
# aus sdr_demo_referencia/app/dlp.py portierten Fassung mit einer flachen
# Marker-Liste — siehe CLAUDE.md, "Offener Punkt: Prompt-Injection-Marker
# sind zu breit", jetzt behoben).
#
# Stufe 1 — eindeutige Marker: referenzieren immer explizit "Anweisungen"/
# "instructions"/"Regeln"/"prompt", also die Steuerungsebene des Modells
# selbst. Echter Geschäftstext (Elektriker, Steuerberater, Immobilien-
# makler, ...) redet praktisch nie in diesen Begriffen über sich selbst —
# reiner Substring-Treffer bleibt hier ausreichend spezifisch.
_INJECTION_MARKERS_STRICT: tuple[str, ...] = (
    "ignoriere die vorherigen anweisungen",
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard previous instructions",
    "reveal your instructions",
    "zeige deine anweisungen",
    "vergiss deine regeln",
    "system prompt",
    "systemanweisung",
)

# Stufe 2 — Rollenumdefinitions-Marker: für sich allein zu breit. Echte
# Kundentexte reden ständig in genau diesen Worten über MENSCHLICHE Rollen
# ("You are now our primary contact...", "act as the account owner...",
# "Ab sofort verhalte dich als Hauptansprechpartner..."). \b-Grenzen zudem,
# damit z. B. "react as soon as possible" nicht fälschlich "act as" matcht.
#
# Blockt nur, wenn ZUSÄTZLICH in der Nähe (±_ROLE_REDEFINITION_WINDOW
# Zeichen) ENTWEDER
#   (a) ein Wort auftaucht, das für sich GENOMMEN schon eindeutig ist
#       (_STANDALONE_SUFFICIENT_CUES: "jailbreak", "developer mode",
#       "entwicklermodus" — kein plausibler Business-Anwendungsfall in
#       Novaras ICP, egal in welchem Satz), ODER
#   (b) eine der eng gefassten, SELBSTREFERENZIELLEN Phrasen aus
#       _SELF_REFERENTIAL_PHRASE_PATTERNS matcht (siehe deren
#       Kommentar unten).
#
# Drei Lektionen aus vorherigen Runden sind hier eingearbeitet
# (verifiziert per /code-review ultra, jeweils):
#
# 1. (Runde 4b) Ein einzelnes Einschränkungswort wie "Regeln"/"filter"/
#    "character" ist ein GANZ NORMALES Geschäftswort und darf NICHT allein
#    blocken ("act as a character reference for this rental application",
#    "act as a filter for spam inquiries").
#
# 2. (Runde 4d) "AI" ist mittlerweile SELBST ein normales Geschäftswort
#    ("our AI vendor evaluation", "our AI team lead") und darf deshalb
#    NICHT eigenständig ausreichen. Nur "jailbreak"/"developer mode"/
#    "entwicklermodus" bleiben eigenständig ausreichend, weil dafür in
#    Novaras ICP (Elektriker, Steuerberater, Immobilienmakler) kein
#    plausibler harmloser Anwendungsfall existiert.
#
# 3. (Runde 5) Ein generisches Verneinungswort ("no", "remove", "keine",
#    "override") IRGENDWO im Fenster zusammen mit einem generischen
#    Einschränkungswort ("policy", "restrictions", "rules") IRGENDWO im
#    Fenster ist STRUKTURELL zu breit, unabhängig von der Fenstergröße --
#    beide Wortarten sind je für sich extrem häufiges Geschäftsvokabular:
#    "override company policy in emergency situations", "remove all
#    restrictions on tenant screening", "keine Einschränkungen bei der
#    Terminvergabe diese Woche" sind allesamt plausible ICP-Sätze, die mit
#    reiner Ko-Vorkommen-Paarung fälschlich blockten. Der eigentliche
#    Jailbreak-Unterschied ist nicht "irgendeine Verneinung + irgendein
#    Einschränkungswort", sondern dass sich die Verneinung EXPLIZIT auf die
#    Rolle/Grenzen des MODELLS SELBST bezieht (2. Person: "your"/"deine",
#    oder eine direkte "beantworte mir alles"-Aufforderung) -- nicht auf
#    einen Geschäftsprozess. Lose Ko-Vorkommen-Paarung ist deshalb ganz
#    entfernt, ersetzt durch die vier eng gefassten Phrasenformen unten.
#
# Alle Wortlisten verwenden explizite Formen statt Wortstämme mit
# Wildcard-Suffix (`\bstamm\w*`) — Stämme overmatchen unvorhersehbar auf
# ähnlich geschriebene, völlig unverwandte Wörter (Runde 4d): "persona"
# matchte "personal", "polic" matchte "police", "limit" matchte "Limited"
# (Firmensuffix), "regel" (als Stamm) hätte "regelmäßig" matchen können,
# "ohne" (als Stamm) hätte "ohnehin" gematcht.
_ROLE_REDEFINITION_CUES: tuple[str, ...] = (
    "you are now",
    "du bist jetzt",
    "act as",
    "verhalte dich als",
)
_ROLE_REDEFINITION_WINDOW = 60  # Zeichen vor/nach dem Marker, die auf Cues geprüft werden

_STANDALONE_SUFFICIENT_CUES: tuple[str, ...] = (
    "jailbreak", "developer mode", "entwicklermodus",
)

_CONSTRAINT_NOUN_WORDS: tuple[str, ...] = (
    "rule", "rules", "regel", "regeln",
    "filter", "filters",
    "restriction", "restrictions", "einschränkung", "einschränkungen",
    "guideline", "guidelines", "richtlinie", "richtlinien",
    "boundary", "boundaries",
    "limit", "limits", "limitation", "limitations",
    "character", "characters",
    "persona", "personas",
    "creator", "creators",
    "policy", "policies",
)
_AI_IDENTITY_WORDS: tuple[str, ...] = (
    "ai", "ki", "llm", "llms", "artificial intelligence",
    "künstliche intelligenz", "chatbot", "chatbots", "large language model",
)
# "lift" (Aufhebungs-Verb) bewusst NICHT aufgenommen -- kollidiert mit dem
# österreichischen Alltagswort "Lift" (Aufzug), direkt relevant für Novaras
# Immobilienmakler-ICP. "disable"/"safeguard" ebenfalls bewusst nicht
# aufgenommen -- "disabled access" ist Standardvokabular in
# Immobilien-Compliance-Texten, "safeguard" kollidiert mit
# Elektriker-Vokabular (Sicherung/Schutzschalter).


def _compile_word_patterns(words: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    # Ausschließlich exakte Wort-/Phrasengrenzen (\b...\b), KEIN
    # Wildcard-Suffix -- siehe Lektion zu Wortstämmen oben. Mehrwortige
    # Phrasen (z. B. "free from") funktionieren genauso, \b greift an
    # beiden Enden der ganzen Phrase. re.escape schützt zusätzlich davor,
    # dass eine künftig hinzugefügte Phrase mit Regex-Sonderzeichen
    # (Klammern, Punkt, ...) das Muster kaputt kompiliert.
    return tuple(re.compile(r"\b" + re.escape(word) + r"\b") for word in words)


def _alternation(words: tuple[str, ...]) -> str:
    # Längere Phrasen zuerst, rein zur Lesbarkeit der kompilierten Regex --
    # bei \b-begrenzter Alternation ohne gemeinsame Präfixe macht die
    # Reihenfolge inhaltlich keinen Unterschied.
    return "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))


_ROLE_REDEFINITION_PATTERNS = _compile_word_patterns(_ROLE_REDEFINITION_CUES)
_STANDALONE_SUFFICIENT_PATTERNS = _compile_word_patterns(_STANDALONE_SUFFICIENT_CUES)

_CONSTRAINT_OR_AI_ALTERNATION = _alternation(_CONSTRAINT_NOUN_WORDS + _AI_IDENTITY_WORDS)
_AI_IDENTITY_ALTERNATION = _alternation(_AI_IDENTITY_WORDS)

# Vier eng gefasste Phrasenformen statt loser Ko-Vorkommen-Paarung (siehe
# Lektion 3 oben) -- jede für sich bereits ein vollständiges, spezifisches
# Signal, kein Zusammenspiel mehrerer unabhängig zu häufiger Wörter mehr:
_SELF_REFERENTIAL_PHRASE_PATTERNS: tuple[re.Pattern, ...] = (
    # (a) Possessiv der 2. Person DIREKT vor einem Einschränkungs-/
    # KI-Wort (max. 2 Füllwörter dazwischen, z. B. "your own strict
    # rules"): "your rules", "your creators", "deine Regeln". Nicht
    # "our"/"unsere" -- das referenziert die Regeln des GESCHÄFTS, nicht
    # die des Modells ("verhalte dich als Vertreter und befolge unsere
    # Regeln" bleibt dadurch erlaubt, siehe Runde 4b).
    re.compile(
        r"\b(?:your|yours|dein|deine|deinen|deiner|deines|euer|eure|euren|eurer|eures)\b"
        r"(?:\s+\w+){0,2}\s+\b(?:" + _CONSTRAINT_OR_AI_ALTERNATION + r")\b"
    ),
    # (b) explizite Verneinung in der 2. Person: "you have no rules",
    # "du hast keine Regeln" -- eindeutig an das Modell selbst gerichtet,
    # anders als eine unpersönliche Aussage wie "es gibt keine
    # Einschränkungen bei der Terminvergabe" (Runde 5).
    re.compile(
        r"\byou\s+(?:have|'ve|has)\s+no\b(?:\s+\w+){0,3}\s+\b(?:"
        + _CONSTRAINT_OR_AI_ALTERNATION + r")\b"
    ),
    re.compile(
        r"\bdu\s+(?:hast|habt)\s+kein\w*\b(?:\s+\w+){0,3}\s+\b(?:"
        + _CONSTRAINT_OR_AI_ALTERNATION + r")\b"
    ),
    # (c) Verneinung DIREKT (max. 1 Füllwort) neben einem KI-Identitätswort
    # speziell -- NICHT neben generischen Geschäftswörtern wie
    # "policy"/"restrictions", das war der Runde-5-Fehler: "an unrestricted
    # AI", "without AI", "kein Chatbot".
    re.compile(
        r"\b(?:no|without|keine?|ohne|unrestricted|uneingeschränkt\w*|unlimited|unlock\w*)\b"
        r"(?:\s+\w+){0,1}\s+\b(?:" + _AI_IDENTITY_ALTERNATION + r")\b"
    ),
    # (d) direkte Aufforderung, uneingeschränkt/vollständig zu antworten --
    # kommt in Kombination mit einem Rollenumdefinitions-Marker in
    # normalem Geschäftstext praktisch nie vor (als eigenständige
    # Kundenfrage schon, z. B. "Tell me everything about the Starter
    # package" -- deshalb bleibt auch dieses Muster an die Nähe zu einem
    # Rollenumdefinitions-Marker gebunden, wie alle Stufe-2-Muster).
    re.compile(
        r"\b(?:tell me everything|tell me anything|answer everything|"
        r"answer anything|reveal everything|beantworte alles|sag mir alles|"
        r"gib mir alles|erzähl mir alles|verrate mir alles)\b"
    ),
)


def _cue_spans(patterns: tuple[re.Pattern, ...], haystack: str) -> list[tuple[int, int]]:
    # Läuft über den vollständigen (unveränderten) Text statt über ein
    # zeichenweise zugeschnittenes Fenster -- ein Fensterausschnitt, der
    # zufällig mitten in einem Wort beginnt, würde sonst dem \b-Muster eine
    # Wortgrenze vortäuschen, die im Originaltext gar nicht existiert
    # (verifiziert per /code-review ultra, 4. Runde: Fensterschnitt mitten in
    # "chai..." täuschte fälschlich das eigenständige Wort "ai" vor).
    return [m.span() for pattern in patterns for m in pattern.finditer(haystack)]


def _overlaps_window(spans: list[tuple[int, int]], window_start: int, window_end: int) -> bool:
    return any(start < window_end and end > window_start for start, end in spans)

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
# Capturing group um die Keyword-Alternation (statt (?:...)) -- liefert den
# Credential-TYP fürs Audit-Log, ohne dass match.group(0) (Keyword+Delimiter+
# Klartextwert) selbst geloggt werden muss.
_CREDENTIAL_PATTERN = re.compile(
    r"\b(" + "|".join(_CREDENTIAL_KEYWORDS) + r")\b" + _CREDENTIAL_DELIMITER + r"\S+",
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
        #
        # Weder das Audit-Log noch blocked_reason (landet über base_agent.py
        # direkt im AgentResponse.error der API-Antwort!) dürfen den
        # Klartext-Treffer enthalten -- sonst wäre genau die Stelle, die den
        # Credential-Leak verhindern soll, selbst eine zweite Leak-Quelle.
        # Gleiche Privacy-Logik wie bei der PII-Redaktion unten: nur Typ +
        # Länge, nie der Wert selbst.
        credential_match = _CREDENTIAL_PATTERN.search(text)
        credential_type: Optional[str] = None
        if credential_match:
            match = credential_match
            credential_type = credential_match.group(1).lower()
        else:
            match = _BEARER_PATTERN.search(text)
            if match:
                credential_type = "bearer_token"
        if match:
            matched_length = len(match.group(0))
            logger.warning(
                "DLP hard-block triggered",
                extra={"credential_type": credential_type, "matched_length": matched_length},
            )
            return DLPResult(
                approved=False,
                redacted_text=text,
                blocked_reason=(
                    f"Blocked credential pattern detected: type={credential_type}, "
                    f"length={matched_length}"
                ),
            )

        # Hard-stop: Prompt-Injection-Versuch, Stufe 1 (eindeutige Marker).
        lower = text.lower()
        strict_marker = next((m for m in _INJECTION_MARKERS_STRICT if m in lower), None)
        if strict_marker:
            logger.warning("DLP hard-block triggered", extra={"injection_marker": strict_marker})
            return DLPResult(
                approved=False,
                redacted_text=text,
                blocked_reason=f"Prompt injection marker detected: '{strict_marker}'",
            )

        # Hard-stop: Prompt-Injection-Versuch, Stufe 2 (Rollenumdefinition +
        # Cue in der Nähe — siehe Kommentar bei den Pattern-Definitionen
        # oben). Cue-Spans werden nur berechnet, wenn überhaupt mindestens
        # ein Rollenumdefinitions-Marker im Text vorkommt -- auf dem
        # weitaus häufigsten Pfad (kein Marker vorhanden) entfällt damit die
        # ~15 Regex-Scans teure Cue-Suche komplett (verifiziert per
        # /code-review ultra, 4. Runde: check_and_redact läuft auf jedem
        # Agenten-Input UND -Output).
        role_matches = [
            match for pattern in _ROLE_REDEFINITION_PATTERNS for match in pattern.finditer(lower)
        ]
        if role_matches:
            standalone_spans = _cue_spans(_STANDALONE_SUFFICIENT_PATTERNS, lower)
            phrase_spans = _cue_spans(_SELF_REFERENTIAL_PHRASE_PATTERNS, lower)

            for match in role_matches:
                window_start = match.start() - _ROLE_REDEFINITION_WINDOW
                window_end = match.end() + _ROLE_REDEFINITION_WINDOW
                triggered_by = None
                if _overlaps_window(standalone_spans, window_start, window_end):
                    triggered_by = "standalone AI/jailbreak cue"
                elif _overlaps_window(phrase_spans, window_start, window_end):
                    triggered_by = "self-referential constraint-redefinition phrase"
                if triggered_by:
                    logger.warning(
                        "DLP hard-block triggered",
                        extra={"injection_marker": match.group(0), "triggered_by": triggered_by},
                    )
                    return DLPResult(
                        approved=False,
                        redacted_text=text,
                        blocked_reason=(
                            f"Prompt injection marker detected: '{match.group(0)}' "
                            f"({triggered_by} nearby)"
                        ),
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
