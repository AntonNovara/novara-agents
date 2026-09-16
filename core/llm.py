"""
Zentrale LLM-Factory für alle Agenten.

Demo-Modus (settings.effective_demo_mode) liefert einen Fake-Client statt
echten API-Calls zu machen — für lokale Entwicklung/Tests ohne Kosten.
Aktiv, wenn kein echter ANTHROPIC_API_KEY gesetzt ist, oder explizit über
DEMO_MODE=true erzwungen (z. B. um beim Entwickeln keine echten Calls zu
verbrauchen, obwohl ein echter Key vorhanden ist).

WICHTIG: Das ist aktuell nur eine Entwicklungs-/Kosten-Bequemlichkeit für den
internen Gebrauch (siehe core/config.py). Sobald ein Agent öffentlich als Demo
exponiert wird, muss Demo-Modus dort zum Sicherheits-Default werden statt nur
zur Bequemlichkeit — das ist ein separater, noch offener Schritt.

Prompt Caching (Sprint 3, 16.09.2026): Alle 5 Text-Agenten betten das volle
Novara-/Mandanten-Wissen (novara_wissen.txt, mehrere tausend Tokens, siehe
core/knowledge.py) in JEDEN System-Prompt ein — Analyse, Persona-Generierung,
Outreach-Text, FAQ-Antwort, etc. laufen alle über denselben, größtenteils
statischen Block. cached_system_message() unten markiert diesen Block mit
Anthropic Prompt Caching (cache_control: ephemeral), sodass wiederholte Calls
mit demselben System-Prompt-Text innerhalb des Cache-Fensters (Default 5 Min.,
serverseitig verwaltet) nur noch die Cache-Read-Rate statt des vollen
Input-Preises zahlen — bis zu ~90 % Ersparnis bei den Token-Kosten, siehe
CLAUDE.md-Roadmap "Prompt Caching". Anthropic ignoriert cache_control
stillschweigend (kein Fehler, keine Zusatzkosten), wenn ein Block die
Mindestlänge fürs Caching unterschreitet — daher ist es sicher, es überall
gleich anzuwenden statt pro Prompt einzeln abzuwägen.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from core.config import settings

logger = logging.getLogger(__name__)

# Generisch-sichere Platzhalterwerte für alle JSON-Felder, die irgendein
# Agenten-Node per LLM anfragt (Extraktion, Analyse, Scoring, Klassifikation).
# Nodes lesen ihre Felder per .get() mit eigenem Fallback, daher genügt eine
# gemeinsame, breite Auswahl — der Inhalt ist irrelevant, nur die Struktur
# muss zu dem passen, was der jeweilige Node erwartet.
_DEMO_JSON_FIELDS: dict[str, Any] = {
    "type": "unknown",
    "confidence": 0.5,
    "company_name": "Demo GmbH",
    "amount": 990.0,
    "currency": "EUR",
    "invoice_date": "01.01.2026",
    "invoice_number": "NA-2026-000",
    "intent": "general",
    "urgency": "low",
    "sentiment": "neutral",
    "language": "de",
    "industry": "other",
    "size_category": "small",
    "icp_score": 50,
    "pain_points": ["(Demo-Modus: keine echte Analyse)"],
    "objections": [],
    "buying_signals": [],
    "next_steps": ["(Demo-Modus: keine echte Analyse)"],
    "deal_stage": "discovery",
    "deal_health_score": 50,
    "close_probability": 50,
    "contact_name": "Demo Kontakt",
    "contact_email": "demo@example.com",
    "meeting_date": "01.01.2026",
    "plan": "starter",
    "team_size": 5,
    "primary_use_case": "(Demo-Modus)",
}

_DEMO_TEXT = (
    "SUBJECT: [Demo-Modus] Platzhalter-Betreff\n\n"
    "Dies ist eine automatisch generierte Platzhalter-Antwort im Demo-Modus "
    "(kein echter LLM-Call). Für echte Inhalte einen echten Anthropic-Schlüssel "
    "hinterlegen und den Demo-Modus deaktivieren."
)


def _extract_text(content: Any) -> str:
    """
    Liest den reinen Text aus einem Message-Content — entweder ein einfacher
    String (Alt-Format) oder eine Liste von Anthropic-Content-Blöcken
    (`[{"type": "text", "text": "...", "cache_control": {...}}]`), wie sie
    cached_system_message() unten erzeugt. _DemoChatModel muss beide Formen
    lesen können, ohne dass die aufrufenden Agenten davon wissen müssen.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return str(content)


def _expects_json(system_text: str) -> bool:
    """
    Erkennt am System-Prompt, ob JSON erwartet wird. Alle Agenten formulieren
    das konsistent ("gib AUSSCHLIESSLICH valides JSON zurück" /
    "Antworte NUR mit dem JSON-Objekt" / "Antworte mit JSON: {...}"),
    während Freitext-Prompts explizit "kein JSON" verlangen — daher die
    Negation zuerst prüfen.
    """
    lower = system_text.lower()
    if "kein json" in lower or "keine json" in lower:
        return False
    return "json" in lower


class _DemoChatModel:
    """
    Minimaler Drop-in-Ersatz für ChatAnthropic im Demo-Modus. Erfüllt nur das
    im Codebase genutzte Subset: .invoke(messages) -> Objekt mit .content.
    """

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        system_text = next(
            (_extract_text(m.content) for m in messages if getattr(m, "type", None) == "system"),
            "",
        )
        if _expects_json(system_text):
            content = json.dumps(_DEMO_JSON_FIELDS, ensure_ascii=False)
        else:
            content = _DEMO_TEXT
        return AIMessage(content=content)


def build_llm(max_tokens: int = 1024) -> Any:
    """
    Zentrale Factory für den LLM-Client aller Agenten. Ersetzt die bisher pro
    Agent duplizierte _build_llm()-Funktion.
    """
    if settings.effective_demo_mode:
        logger.info("LLM-Factory: Demo-Modus aktiv, kein echter API-Call")
        return _DemoChatModel()
    return ChatAnthropic(
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key.get_secret_value(),
        temperature=0,
        max_tokens=max_tokens,
    )


def cached_system_message(text: str) -> SystemMessage:
    """
    Baut eine SystemMessage mit aktiviertem Anthropic Prompt Caching auf dem
    Text-Block (`cache_control: {"type": "ephemeral"}`). Alle Agenten-Nodes
    sollen ihre System-Prompts über diese Funktion statt über ein direktes
    `SystemMessage(content=text)` erzeugen — siehe Modul-Docstring oben.

    Wirkt sowohl mit dem echten ChatAnthropic-Client (der Content-Block-Listen
    inkl. cache_control 1:1 an die Anthropic-API weiterreicht) als auch mit
    _DemoChatModel (liest den Text über _extract_text() aus der Blockliste).
    """
    return SystemMessage(
        content=[{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
    )
