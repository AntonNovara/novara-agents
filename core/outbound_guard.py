"""
Outbound-Guard -- deterministische Prüfung jeder ausgehenden Erstnachricht,
BEVOR sie an CRM/Sequenz/Anton weitergereicht wird.

Warum deterministisch statt "LLM-als-Richter": Die Regeln hier sind harte
Geschäftsregeln (nur echte Preise, keine Platzhalter, keine Garantieversprechen,
Opt-out-Hinweis). Ein zweites LLM würde diese Regeln nur wahrscheinlich, nicht
sicher durchsetzen -- und Tokens kosten. Ein LLM-Judge kann später NUR Stil/Ton
bewerten, nie diese Regeln aufweichen.

Preise werden nicht hartkodiert, sondern aus novara_wissen.txt gelesen -- wer
dort Preise ändert, ändert automatisch die Erlaubnisliste (kein Drift).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.knowledge import load_novara_wissen

OPT_OUT_LINE_DE = "Kein Interesse? Kurze Antwort genügt, dann melde ich mich nicht mehr."

_PLACEHOLDER = re.compile(
    r"(\[Demo-Modus|Platzhalter|\[\s*(Name|Firma|Vorname|Betrieb)\s*\]|\{\{?[a-z_]+\}?\}|XXX|lorem ipsum)",
    re.IGNORECASE,
)
_BRACKET_PLACEHOLDER = re.compile(r"\[[^\]\n]{2,40}\]")          # z. B. "[Datum]", "[Ihr Name]"
_MARKDOWN = re.compile(r"(^\s*#{1,6}\s|\*\*[^*\n]+\*\*)", re.MULTILINE)  # Überschriften/Fettdruck gehören nicht in eine Mail
_MULTI_MESSAGE = re.compile(r"(folge-?mail|follow-?up|nachfass|tag\s*\d+\s*[—–-]|\n---+\n)", re.IGNORECASE)
_PROMISES = re.compile(
    r"(garantiert(e|en)?\s+(mehr|dass|umsatz|auftr)|100\s?%\s*(sicher|garant|erfolg)|risikofrei|"
    r"sicher(e|en)?\s+mehr\s+auftr)",
    re.IGNORECASE,
)
_LLM_LEAKAGE = re.compile(
    r"(as an ai language model|system prompt|ignore (all )?previous|als ki-sprachmodell|"
    r"wissensdatenbank|<\/?(system|assistant)>)",
    re.IGNORECASE,
)
_OPT_OUT = re.compile(
    r"(kein interesse|nicht mehr melden|abmelden|austragen|keine weiteren nachrichten|widerspr|unsubscribe)",
    re.IGNORECASE,
)
_EURO = re.compile(r"€\s?(\d[\d.]*)|(\d[\d.]*)\s?(?:€|EUR|Euro)\b")

# "unser/unsere ... -System/-Software/-Lösung/-App/-Tool/-Plattform": wer hier etwas
# Konkretes anbietet, darf nur Begriffe nennen, die in novara_wissen.txt vorkommen.
# Erfundene Angebote (z. B. "unser Kassen-System" für eine Bäckerei) sind der
# gefährlichste Fehler kleiner Modelle.
_OFFER_PHRASE = re.compile(
    r"\bunser(?:e[nrms]?)?\s+((?:[\w\-äöüÄÖÜß]+\s+){0,4}?[\w\-äöüÄÖÜß]*(?:system|software|lösung|app|tool|plattform|assistent|bot)\b)",
    re.IGNORECASE,
)
_GENERIC_OFFER_WORDS = {"system", "software", "lösung", "app", "tool", "plattform", "assistent", "bot",
                        "team", "service", "angebot", "ki", "automatisch", "automatische", "automatischen",
                        "automatisches", "intelligent", "intelligente", "intelligenten", "intelligentes"}

MAX_CHARS = 1500


def has_opt_out(body: str) -> bool:
    return _OPT_OUT.search(body) is not None


def _amount(raw: str) -> int:
    return int(raw.replace(".", ""))


def _allowed_prices() -> set[int]:
    prices: set[int] = set()
    for m in _EURO.finditer(load_novara_wissen()):
        prices.add(_amount(m.group(1) or m.group(2)))
    return prices


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", text)}


def _unknown_offer_terms(text: str) -> list[str]:
    vocab = _tokens(load_novara_wissen())
    unknown: list[str] = []
    for m in _OFFER_PHRASE.finditer(text):
        for tok in _tokens(m.group(1)):
            base = re.sub(r"(system|software|lösung|app|tool|plattform|assistent|bot)$", "", tok)
            if tok in _GENERIC_OFFER_WORDS or not base or tok in vocab or base in vocab:
                continue
            unknown.append(tok)
    return sorted(set(unknown))


@dataclass
class GuardVerdict:
    ok: bool
    violations: list[str] = field(default_factory=list)   # blockierend
    warnings: list[str] = field(default_factory=list)     # nur Hinweis


def review_outreach(subject: str, body: str) -> GuardVerdict:
    text = f"{subject}\n{body}"
    v: list[str] = []
    w: list[str] = []

    if not body.strip():
        v.append("leere Nachricht")
    if _PLACEHOLDER.search(text):
        v.append("Platzhalter/Demo-Text in der Nachricht")
    if _BRACKET_PLACEHOLDER.search(text):
        v.append("unausgefüllter Platzhalter in eckigen Klammern")
    if _MARKDOWN.search(text):
        v.append("Markdown-Formatierung in der Nachricht")
    if _MULTI_MESSAGE.search(body):
        v.append("mehrere Nachrichten/Follow-ups in einer Nachricht")
    if _LLM_LEAKAGE.search(text):
        v.append("LLM-/Prompt-Artefakt in der Nachricht")
    if _PROMISES.search(text):
        v.append("unzulässiges Erfolgs-/Garantieversprechen")

    allowed = _allowed_prices()
    for m in _EURO.finditer(text):
        amount = _amount(m.group(1) or m.group(2))
        if amount not in allowed:
            v.append(f"Preis €{amount} steht nicht in novara_wissen.txt")

    unknown = _unknown_offer_terms(body)
    if unknown:
        v.append(f"Angebot nicht in novara_wissen.txt belegt: {', '.join(unknown)}")

    if not has_opt_out(body):
        v.append("kein Opt-out-Hinweis")
    if len(body) > MAX_CHARS:
        w.append(f"Nachricht sehr lang ({len(body)} Zeichen)")

    return GuardVerdict(ok=not v, violations=v, warnings=w)
