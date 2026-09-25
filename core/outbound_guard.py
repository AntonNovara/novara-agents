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
    if _LLM_LEAKAGE.search(text):
        v.append("LLM-/Prompt-Artefakt in der Nachricht")
    if _PROMISES.search(text):
        v.append("unzulässiges Erfolgs-/Garantieversprechen")

    allowed = _allowed_prices()
    for m in _EURO.finditer(text):
        amount = _amount(m.group(1) or m.group(2))
        if amount not in allowed:
            v.append(f"Preis €{amount} steht nicht in novara_wissen.txt")

    if not has_opt_out(body):
        v.append("kein Opt-out-Hinweis")
    if len(body) > MAX_CHARS:
        w.append(f"Nachricht sehr lang ({len(body)} Zeichen)")

    return GuardVerdict(ok=not v, violations=v, warnings=w)
