"""
Sales Copilot Agent – vollständig implementiert.

Workflow (LangGraph StateGraph):
  parse_transcript     ← LLM: Firma, Kontakt, Datum, Deal-Stage, Zusammenfassung
        ↓
  detect_signals       ← LLM: Objections (Text + Kategorie + Schwere),
        ↓                      Buying Signals, Next Steps, Deal-Health-Score
  compose_followup     ← LLM: personalisierte Follow-up-E-Mail mit
        ↓                      SUBJECT-Zeile, adressiert Einwände, bestätigt Next Steps
  update_deal          ← DealTracker.upsert_deal()  (Mock → HubSpot / Salesforce)
        ↓
  finalize
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from agents.base_agent import AgentRequest, BaseAgent
from core.config import settings
from core.knowledge import load_novara_wissen
from tools.deal_tracker import DealRecord, DealStage, DealTracker

logger = logging.getLogger(__name__)

_WISSEN = load_novara_wissen()


# ── LLM singleton ─────────────────────────────────────────────────────────────

def _build_llm() -> ChatAnthropic:
    return ChatAnthropic(
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key.get_secret_value(),
        temperature=0,
        max_tokens=1024,
    )


def _parse_llm_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return json.loads(text.strip())


# ── Graph State ────────────────────────────────────────────────────────────────

class SalesCopilotState(TypedDict):
    input_text: str
    session_id: str

    # set by parse_transcript
    company_name: str
    contact_name: str
    contact_title: str
    meeting_date: str
    deal_stage: str       # discovery | demo | proposal | negotiation | closing
    meeting_summary: str
    language: str         # de | en

    # set by detect_signals
    objections: list[dict]    # [{text, category, severity}]
    buying_signals: list[str]
    next_steps: list[str]
    deal_health_score: int    # 0-100
    close_probability: int    # 0-100

    # set by compose_followup
    followup_subject: str
    followup_body: str

    # set by update_deal
    deal_result: dict

    # final
    final_result: dict
    error: Optional[str]


# ── LLM Prompts ────────────────────────────────────────────────────────────────

_SYSTEM_PARSE = f"""\
Du bist ein Sales-Analyst bei Novara Automation.
Novara ist ein österreichisches Unternehmen (Wien) das Handwerksbetrieben hilft,
manuelle Prozesse zu automatisieren.

=== NOVARA WISSENSDATENBANK (Verkaufsprozess, Phasen, Pakete) ===
{_WISSEN}
=== ENDE WISSENSDATENBANK ===

Extrahiere strukturierte Daten aus Gesprächsnotizen oder einem Transkript.
Gib AUSSCHLIESSLICH valides JSON zurück:
{{
  "company_name": string,
  "contact_name": string,
  "contact_title": string (bei Handwerksbetrieben oft "Inhaber" oder "Geschäftsführer"),
  "meeting_date": string (ISO-Datum YYYY-MM-DD oder "unknown"),
  "deal_stage": eines von ["discovery","demo","proposal","negotiation","closing"],
  "meeting_summary": string (2-3 Sätze die den Kern des Gesprächs erfassen),
  "language": "de" | "en"
}}

Deal-Stage-Mapping gemäß Novara-Verkaufsprozess:
  discovery    = Erstkontakt / Advice-Call / Hauptschmerz identifizieren
  demo         = Produkt / Lösung gezeigt oder vorgeführt
  proposal     = Angebot versendet (Starter €990 oder Growth €2.490)
  negotiation  = Vertragsbedingungen, Anzahlung besprochen
  closing      = Unterschrift unmittelbar bevorstehend
"""

_SYSTEM_DETECT = f"""\
Du bist ein Sales-Signal-Analyst bei Novara Automation.
Analysiere Gesprächsnotizen/Transkript und gib AUSSCHLIESSLICH valides JSON zurück:
{{
  "objections": [
    {{"text": string, "category": eines von ["pricing","timing","competitor","authority","need","trust","complexity"], "severity": "low"|"medium"|"high"}}
  ],
  "buying_signals": [string],
  "next_steps": [string],
  "deal_health_score": integer 0-100,
  "close_probability": integer 0-100
}}

=== TYPISCHE EINWÄNDE UND ANTWORTEN AUS WISSENSDATENBANK ===
{_WISSEN}
=== ENDE ===

Einwand-Kategorien für Novara-Kontext:
  pricing    = "Zu teuer", "Kann ich mir nicht leisten", "Weiß nicht ob sich das rechnet"
  timing     = "Keine Zeit", "Ruf mich nächsten Monat an", "Viel um die Ohren"
  competitor = Nutzt bereits eine andere Software / Lösung
  authority  = Inhaber nicht erreichbar / entscheidet nicht alleine
  need       = "Läuft gut bei uns", "Brauchen das nicht", "Meine Frau macht das"
  trust      = "Das funktioniert bei uns nicht", "Hab das probiert", "Vertrau sowas nicht"
  complexity = "Zu kompliziert", "Wir haben schon eine Software"

Deal-Health-Scoring:
  Start bei 50. +10 pro starkem Kaufsignal (max +30). -10 pro high-Einwand,
  -5 pro medium (max -30). +15 bei konkreten Next Steps. -15 bei keinen Next Steps.
  Stage-Bonus: demo +5, proposal +10, negotiation +15, closing +20.
"""

_SYSTEM_FOLLOWUP = f"""\
Du bist Anton, Geschäftsführer von Novara Automation, und schreibst eine
Post-Meeting-Follow-up-E-Mail.

=== NOVARA WISSENSDATENBANK (Ton, Einwandbehandlung, Vorlagen) ===
{_WISSEN}
=== ENDE WISSENSDATENBANK ===

WICHTIGE SPRACHREGELN (strikt einhalten):
1. Einstieg: spezifisch — etwas Konkretes aus DIESEM Gespräch referenzieren
   (nicht "Danke für Ihre Zeit" oder "Es war schön...")
2. Fasse 2-3 wichtigste Gesprächspunkte in 1-2 Zeilen zusammen
3. Für jeden high/medium-Einwand: anerkennen ("Das verstehe ich") und konstruktiv
   weiterdrehen — nie verteidigen, immer mit einer Frage
4. Bestätige jeden vereinbarten Next Step mit Verantwortlichkeit
5. Schließe mit GENAU EINER weichen CTA
6. Max 10 Zeilen Body. Betreff: max 8 Wörter, spezifisch für diese Firma.
7. Ton: kollegial, neugierig, nicht drängend — österreichisches Deutsch, "Sie"-Form
8. Unterschrift: "Freundliche Grüße, Anton" oder "LG, Anton"
9. KEIN Technik-Jargon: kein "KI", kein "Make.com"

Sprache: {{language}}

Format:
  SUBJECT: <Betreff>
  (Leerzeile)
  <Body>
"""


# ── Graph ──────────────────────────────────────────────────────────────────────

class SalesCopilotGraph:

    def __init__(self, llm: ChatAnthropic, tracker: DealTracker) -> None:
        self._llm = llm
        self._tracker = tracker
        self._graph = self._build_graph()

    # ── Node: parse_transcript ───────────────────────────────────────────────

    def parse_transcript(self, state: SalesCopilotState) -> SalesCopilotState:
        logger.info("Node: parse_transcript", extra={"session": state["session_id"]})

        defaults: dict[str, Any] = {
            "company_name": "Unknown Company",
            "contact_name": "Unknown Contact",
            "contact_title": "Unknown",
            "meeting_date": "unknown",
            "deal_stage": "discovery",
            "meeting_summary": state["input_text"][:120],
            "language": "de",
        }
        try:
            response = self._llm.invoke([
                SystemMessage(content=_SYSTEM_PARSE),
                HumanMessage(content=state["input_text"]),
            ])
            data = _parse_llm_json(response.content)
            return {
                **state,
                "company_name":    data.get("company_name", defaults["company_name"]),
                "contact_name":    data.get("contact_name", defaults["contact_name"]),
                "contact_title":   data.get("contact_title", defaults["contact_title"]),
                "meeting_date":    data.get("meeting_date", defaults["meeting_date"]),
                "deal_stage":      data.get("deal_stage", defaults["deal_stage"]),
                "meeting_summary": data.get("meeting_summary", defaults["meeting_summary"]),
                "language":        data.get("language", defaults["language"]),
            }
        except Exception as exc:
            logger.warning("parse_transcript LLM failed: %s", exc)
            return {**state, **defaults}

    # ── Node: detect_signals ─────────────────────────────────────────────────

    def detect_signals(self, state: SalesCopilotState) -> SalesCopilotState:
        logger.info("Node: detect_signals", extra={"session": state["session_id"]})

        defaults: dict[str, Any] = {
            "objections": [],
            "buying_signals": [],
            "next_steps": [],
            "deal_health_score": 50,
            "close_probability": 30,
        }
        try:
            response = self._llm.invoke([
                SystemMessage(content=_SYSTEM_DETECT),
                HumanMessage(content=state["input_text"]),
            ])
            data = _parse_llm_json(response.content)
            return {
                **state,
                "objections":        data.get("objections", []),
                "buying_signals":    data.get("buying_signals", []),
                "next_steps":        data.get("next_steps", []),
                "deal_health_score": int(data.get("deal_health_score", 50)),
                "close_probability": int(data.get("close_probability", 30)),
            }
        except Exception as exc:
            logger.warning("detect_signals LLM failed: %s", exc)
            return {**state, **defaults}

    # ── Node: compose_followup ───────────────────────────────────────────────

    def compose_followup(self, state: SalesCopilotState) -> SalesCopilotState:
        logger.info("Node: compose_followup", extra={"session": state["session_id"]})

        objection_lines = "\n".join(
            f"  • [{o.get('severity','?').upper()}] {o.get('category','')}: {o.get('text','')}"
            for o in state["objections"]
        ) or "  (keine identifiziert)"

        signal_lines = "\n".join(f"  • {s}" for s in state["buying_signals"]) or "  (keine identifiziert)"
        step_lines   = "\n".join(f"  • {s}" for s in state["next_steps"]) or "  (keine vereinbart)"

        context = (
            f"Company:          {state['company_name']}\n"
            f"Contact:          {state['contact_name']}, {state['contact_title']}\n"
            f"Meeting date:     {state['meeting_date']}\n"
            f"Deal stage:       {state['deal_stage']}\n"
            f"Summary:          {state['meeting_summary']}\n\n"
            f"Objections:\n{objection_lines}\n\n"
            f"Buying signals:\n{signal_lines}\n\n"
            f"Agreed next steps:\n{step_lines}\n\n"
            f"Deal health: {state['deal_health_score']}/100 | "
            f"Close probability: {state['close_probability']}%"
        )

        fallback_subject = f"Nächste Schritte nach unserem Gespräch – {state['company_name']}"
        fallback_body = (
            f"Hallo {state['contact_name'].split()[0]},\n\n"
            f"vielen Dank für das heutige Gespräch. Kurze Zusammenfassung:\n\n"
            f"{state['meeting_summary']}\n\n"
            + (f"Vereinbarte nächste Schritte:\n{step_lines}\n\n" if state["next_steps"] else "")
            + "Bei Fragen stehe ich jederzeit zur Verfügung.\n\nViele Grüße"
        )

        try:
            lang = "German" if state["language"] == "de" else "English"
            response = self._llm.invoke([
                SystemMessage(content=_SYSTEM_FOLLOWUP.format(language=lang)),
                HumanMessage(content=context),
            ])
            raw = response.content.strip()
        except Exception as exc:
            logger.warning("compose_followup LLM failed: %s", exc)
            return {**state, "followup_subject": fallback_subject, "followup_body": fallback_body}

        subject, body = fallback_subject, raw
        if raw.upper().startswith("SUBJECT:"):
            lines = raw.split("\n", 2)
            subject = lines[0].split(":", 1)[1].strip()
            body = lines[2].strip() if len(lines) > 2 else raw

        return {**state, "followup_subject": subject, "followup_body": body}

    # ── Node: update_deal ────────────────────────────────────────────────────

    def update_deal(self, state: SalesCopilotState) -> SalesCopilotState:
        logger.info("Node: update_deal", extra={"session": state["session_id"]})

        stage_map = {s.value: s for s in DealStage}
        stage = stage_map.get(state["deal_stage"], DealStage.DISCOVERY)

        record = DealRecord(
            company_name=state["company_name"],
            contact_name=state["contact_name"],
            contact_title=state["contact_title"],
            deal_stage=stage,
            deal_health_score=state["deal_health_score"],
            close_probability=state["close_probability"],
            objections=state["objections"],
            buying_signals=state["buying_signals"],
            next_steps=state["next_steps"],
            meeting_summary=state["meeting_summary"],
            followup_subject=state["followup_subject"],
            followup_body=state["followup_body"],
        )
        result = self._tracker.upsert_deal(record)
        return {**state, "deal_result": result.model_dump()}

    # ── Node: finalize ───────────────────────────────────────────────────────

    def finalize(self, state: SalesCopilotState) -> SalesCopilotState:
        logger.info("Node: finalize", extra={"session": state["session_id"]})

        final: dict[str, Any] = {
            "meeting": {
                "company":  state["company_name"],
                "contact":  f"{state['contact_name']}, {state['contact_title']}",
                "date":     state["meeting_date"],
                "stage":    state["deal_stage"],
                "summary":  state["meeting_summary"],
            },
            "analysis": {
                "objections":        state["objections"],
                "buying_signals":    state["buying_signals"],
                "next_steps":        state["next_steps"],
                "deal_health_score": state["deal_health_score"],
                "close_probability": state["close_probability"],
            },
            "followup_email": {
                "subject": state["followup_subject"],
                "body":    state["followup_body"],
            },
            "deal": state["deal_result"],
        }
        return {**state, "final_result": final, "error": None}

    # ── Graph Builder ─────────────────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(SalesCopilotState)

        graph.add_node("parse_transcript", self.parse_transcript)
        graph.add_node("detect_signals",   self.detect_signals)
        graph.add_node("compose_followup", self.compose_followup)
        graph.add_node("update_deal",      self.update_deal)
        graph.add_node("finalize",         self.finalize)

        graph.set_entry_point("parse_transcript")
        graph.add_edge("parse_transcript", "detect_signals")
        graph.add_edge("detect_signals",   "compose_followup")
        graph.add_edge("compose_followup", "update_deal")
        graph.add_edge("update_deal",      "finalize")
        graph.add_edge("finalize",         END)

        return graph.compile()

    def run(self, input_text: str, session_id: str) -> dict[str, Any]:
        initial: SalesCopilotState = {
            "input_text":        input_text,
            "session_id":        session_id,
            "company_name":      "",
            "contact_name":      "",
            "contact_title":     "",
            "meeting_date":      "",
            "deal_stage":        "",
            "meeting_summary":   "",
            "language":          "de",
            "objections":        [],
            "buying_signals":    [],
            "next_steps":        [],
            "deal_health_score": 0,
            "close_probability": 0,
            "followup_subject":  "",
            "followup_body":     "",
            "deal_result":       {},
            "final_result":      {},
            "error":             None,
        }
        return self._graph.invoke(initial)["final_result"]


# ── SalesCopilotAgent ─────────────────────────────────────────────────────────

class SalesCopilotAgent(BaseAgent):
    """Analysiert Verkaufsgespräche und generiert Follow-up-E-Mail + Deal-Update."""

    agent_type = "sales-copilot"

    def __init__(self) -> None:
        super().__init__()
        self._workflow = SalesCopilotGraph(
            llm=_build_llm(),
            tracker=DealTracker(endpoint=settings.crm_endpoint),
        )

    def _run(self, request: AgentRequest) -> dict[str, Any]:
        return self._workflow.run(
            input_text=request.text,
            session_id=request.session_id,
        )
