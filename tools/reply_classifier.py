"""
Reply Classifier – klassifiziert eingehende Antworten auf SDR-Outreach.

Kategorien: interested | objection | opt_out | unclear.

Opt-out wird NICHT dem LLM überlassen: ein Regex/Keyword-Hard-Match läuft
ZUERST (gleiche Philosophie wie core/security.py — ein Fall mit
DSGVO/ePrivacy-Konsequenz braucht eine deterministische Garantie, keine
Wahrscheinlichkeit). Nur wenn kein Opt-out-Muster greift, entscheidet ein
LLM-Fallback zwischen "interested" und "objection" (core.llm.build_llm(),
inkl. Demo-Modus wie alle Agenten — kein Kosten-/Netzwerk-Aufwand ohne
echten Anthropic-Key).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from core.llm import build_llm

logger = logging.getLogger(__name__)

ReplyIntent = str  # "interested" | "objection" | "opt_out" | "unclear"

# Deutsch + Englisch — Novaras ICP ist DACH, aber der Rest des Systems
# (z. B. core/security.py-Tests) behandelt durchgehend beide Sprachen.
_OPT_OUT_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bbitte\s+keine?\b(?:\s+\w+){0,3}\s+\b(?:mails?|e-?mails?|nachrichten)\b",
        r"\bbitte\s+nicht\s+mehr\b(?:\s+\w+){0,4}\s+\bkontaktieren\b",
        r"\bkeine\s+weiteren?\s+(?:mails?|e-?mails?|nachrichten|anrufe)\b",
        r"\bnicht\s+mehr\s+kontaktieren\b",
        r"\bvon\s+(?:der|dieser)\s+liste\s+(?:streichen|entfernen)\b",
        r"\babmeldung\b",
        r"\bmelden?\s+sie\s+mich\s+ab\b",
        r"\bunsubscribe\b",
        r"\bstop\s+(?:contacting|emailing|calling)\s+me\b",
        r"\bplease\s+(?:do\s+not|don't)\s+(?:email|contact|call)\s+me\s+again\b",
        r"\bremove\s+me\s+from\s+(?:your|this)\s+list\b",
    )
)

_SYSTEM_CLASSIFY = """\
Du analysierst die Antwort eines B2B-Kontakts auf eine Kaltakquise-Nachricht \
(E-Mail oder LinkedIn) eines Handwerksbetrieb-Anbieters (Novara Automation).

Klassifiziere AUSSCHLIESSLICH in eine von zwei Kategorien:
- "interested": echtes Interesse, Rückfrage zu Preis/Produkt, Terminwunsch,
  "erzählen Sie mir mehr", positive Reaktion.
- "objection": Ablehnung oder Einwand, der die Tür nicht endgültig schließt
  ("gerade kein Budget", "aktuell keine Zeit", "kein Bedarf", "später
  vielleicht", Skepsis, Rückfrage zu Referenzen/Sicherheit).

(Explizite Abmeldewünsche werden bereits VOR dir separat erkannt und
erreichen dich nie — antworte daher nie mit "opt_out".)

Gib AUSSCHLIESSLICH valides JSON zurück:
{
  "intent": "interested" | "objection",
  "confidence": float zwischen 0 und 1,
  "rationale": "1 Satz Begründung auf Deutsch"
}
"""


@dataclass
class ClassificationResult:
    intent: ReplyIntent
    confidence: float
    rationale: str
    matched_pattern: Optional[str] = None  # nur bei opt_out gesetzt (Audit-Trail)


def _parse_llm_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return json.loads(text.strip())


class ReplyClassifier:
    def __init__(self, llm: Any = None) -> None:
        self._llm = llm if llm is not None else build_llm(max_tokens=200)

    def classify(self, text: str) -> ClassificationResult:
        for pattern in _OPT_OUT_PATTERNS:
            match = pattern.search(text)
            if match:
                logger.info("Reply classified as opt_out (hard match)", extra={"pattern": pattern.pattern})
                return ClassificationResult(
                    intent="opt_out",
                    confidence=1.0,
                    rationale="Deterministisches Opt-out-Muster erkannt",
                    matched_pattern=match.group(0),
                )
        return self._classify_with_llm(text)

    def _classify_with_llm(self, text: str) -> ClassificationResult:
        try:
            response = self._llm.invoke(
                [SystemMessage(content=_SYSTEM_CLASSIFY), HumanMessage(content=text)]
            )
            data = _parse_llm_json(response.content)
            intent = data.get("intent", "unclear")
            if intent not in ("interested", "objection"):
                intent = "unclear"
            return ClassificationResult(
                intent=intent,
                confidence=float(data.get("confidence", 0.5)),
                rationale=data.get("rationale", ""),
            )
        except Exception as exc:
            logger.warning("Reply-Klassifikation per LLM fehlgeschlagen: %s", exc)
            return ClassificationResult(intent="unclear", confidence=0.0, rationale=f"LLM-Fehler: {exc}")
