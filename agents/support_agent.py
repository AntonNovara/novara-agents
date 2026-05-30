"""
Support Agent – vollständig implementiert.

Workflow (LangGraph StateGraph):
  analyze_inquiry        ← LLM: Intent / Urgency / Sentiment / Language
        ↓
  search_faq             ← FAQDatabase (Keyword-Suche mit Prefix-Stemming)
        ↓
  _route_after_faq
        ├── confidence ≥ 0.30 AND kein forced_escalate
        │         ↓
        │   compose_faq_response  ← LLM: personalisierte Antwort im
        │         ↓                       Stil des erkannten Sprachraums
        │     finalize
        │
        └── sonst
                ↓
          create_ticket   ← TicketSystem (Mock → in Prod: Zendesk / Jira SD)
                ↓
            finalize
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from agents.base_agent import AgentRequest, BaseAgent
from core.config import settings
from core.knowledge import load_novara_wissen
from tools.faq_database import FAQDatabase, FAQSearchResult
from tools.ticket_system import TicketPriority, TicketRecord, TicketSystem


def _parse_llm_json(text: str) -> dict:
    """Parse JSON from LLM output, stripping markdown code fences if present."""
    text = text.strip()
    if text.startswith("```"):
        # Drop opening fence (```json or ```)
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        # Drop closing fence
        text = re.sub(r"\n?```\s*$", "", text)
    return json.loads(text.strip())

logger = logging.getLogger(__name__)

_WISSEN = load_novara_wissen()


# ── LLM singleton ─────────────────────────────────────────────────────────────

def _build_llm() -> ChatAnthropic:
    return ChatAnthropic(
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key.get_secret_value(),
        temperature=0,
        max_tokens=512,
    )


# ── Graph State ────────────────────────────────────────────────────────────────

class SupportState(TypedDict):
    input_text: str
    session_id: str

    # set by analyze_inquiry
    intent: str          # billing | technical | onboarding | privacy | support_hours | complaint | general
    urgency: str         # low | medium | high
    sentiment: str       # positive | neutral | negative
    language: str        # de | en
    inquiry_summary: str

    # set by search_faq
    faq_results: list[dict]
    faq_confidence: float  # score of the top result (0.0 if no hit)

    # set by routing
    action: str          # faq_response | ticket_created
    forced_escalate: bool

    # set by compose_faq_response
    faq_answer: str

    # set by create_ticket
    ticket_result: dict

    # final
    final_result: dict
    error: Optional[str]


# ── LLM Prompts ────────────────────────────────────────────────────────────────

_SYSTEM_ANALYZE = f"""\
Du bist ein Support-Klassifikator bei Novara Automation.
Novara Automation ist ein österreichisches Unternehmen (Wien) das Handwerksbetrieben
(Schwerpunkt Elektrikerbetriebe) hilft, manuelle Prozesse zu automatisieren.

=== NOVARA WISSENSDATENBANK ===
{_WISSEN}
=== ENDE WISSENSDATENBANK ===

Analysiere die eingehende Kundenanfrage und gib AUSSCHLIESSLICH valides JSON zurück:
{{
  "intent": eines von ["billing","technical","onboarding","privacy","support_hours","complaint","general"],
  "urgency": eines von ["low","medium","high"],
  "sentiment": eines von ["positive","neutral","negative"],
  "language": eines von ["de","en"],
  "summary": eine Zeile (max 20 Wörter) was der Kunde möchte
}}

Regeln:
  - "complaint" = Kunde äußert Unzufriedenheit oder Ärger
  - "billing" = Fragen zu Preisen (Starter €990, Growth €2.490, Retainer €590/Monat),
    Rechnungen (RE-2026-xxx), Zahlungsmodell (50/50)
  - urgency "high" = Kunde ist blockiert, System ausgefallen, Datenverlust
  - language "de" wenn Anfrage primär auf Deutsch, sonst "en"

Antworte NUR mit dem JSON-Objekt, keine Erklärung.
"""

_SYSTEM_COMPOSE = f"""\
Du bist ein freundlicher Support-Agent bei Novara Automation.
Novara hilft Handwerksbetrieben (Elektriker, Installateure, etc.) in Wien/Österreich,
Prozesse wie verpasste Anrufe, Angebotserstellung und Terminbestätigung zu automatisieren.

=== NOVARA WISSENSDATENBANK (für präzise Antworten zu Paketen, Preisen, Prozessen) ===
{_WISSEN}
=== ENDE WISSENSDATENBANK ===

Ein relevanter FAQ-Eintrag wurde gefunden. Schreibe eine hilfreiche, präzise Antwort die:
  - Die spezifische Frage des Kunden direkt beantwortet
  - Den FAQ-Eintrag als primäre Informationsquelle nutzt
  - Bei Preisfragen die echten Novara-Pakete (Starter/Growth/Retainer) nennt
  - In der unten angegebenen Sprache geschrieben ist ({{language}})
  - Professionell aber zugänglich ist — österreichisches Deutsch, "Sie"-Form
  - Maximal 2–5 Sätze; die Kundenfrage nicht wiederholen

Antworte NUR mit dem Antworttext, kein JSON, keine Präambel.
"""


# ── Graph ──────────────────────────────────────────────────────────────────────

class SupportGraph:
    """LangGraph-Workflow für den Support Agent."""

    def __init__(self, llm: ChatAnthropic, faq: FAQDatabase, tickets: TicketSystem) -> None:
        self._llm = llm
        self._faq = faq
        self._tickets = tickets
        self._graph = self._build_graph()

    # ── Node: analyze_inquiry ────────────────────────────────────────────────

    def analyze_inquiry(self, state: SupportState) -> SupportState:
        logger.info("Node: analyze_inquiry", extra={"session": state["session_id"]})

        defaults = {
            "intent": "general",
            "urgency": "medium",
            "sentiment": "neutral",
            "language": "de",
            "inquiry_summary": state["input_text"][:80],
        }

        try:
            response = self._llm.invoke([
                SystemMessage(content=_SYSTEM_ANALYZE),
                HumanMessage(content=state["input_text"]),
            ])
            data: dict = _parse_llm_json(response.content)
            return {
                **state,
                "intent": data.get("intent", defaults["intent"]),
                "urgency": data.get("urgency", defaults["urgency"]),
                "sentiment": data.get("sentiment", defaults["sentiment"]),
                "language": data.get("language", defaults["language"]),
                "inquiry_summary": data.get("summary", defaults["inquiry_summary"]),
            }
        except Exception as exc:
            logger.warning("analyze_inquiry LLM failed, using defaults: %s", exc)
            return {**state, **defaults}

    # ── Node: search_faq ─────────────────────────────────────────────────────

    def search_faq(self, state: SupportState) -> SupportState:
        logger.info("Node: search_faq", extra={"session": state["session_id"]})

        results: list[FAQSearchResult] = self._faq.search(state["input_text"], top_k=3)

        serialized = [
            {
                "id": r.entry.id,
                "category": r.entry.category,
                "question": r.entry.question,
                "answer": r.entry.answer,
                "score": r.score,
                "matched_keywords": r.matched_keywords,
            }
            for r in results
        ]

        top_confidence = results[0].score if results else 0.0
        forced = (
            state["intent"] == "complaint"
            and state["urgency"] == "high"
        )

        logger.debug(
            "FAQ search result",
            extra={"confidence": top_confidence, "forced_escalate": forced},
        )

        return {
            **state,
            "faq_results": serialized,
            "faq_confidence": top_confidence,
            "forced_escalate": forced,
        }

    # ── Node: compose_faq_response ───────────────────────────────────────────

    def compose_faq_response(self, state: SupportState) -> SupportState:
        logger.info("Node: compose_faq_response", extra={"session": state["session_id"]})

        top = state["faq_results"][0]
        lang_label = "German" if state["language"] == "de" else "English"

        prompt = (
            f"Language to use: {lang_label}\n\n"
            f"FAQ entry:\nQ: {top['question']}\nA: {top['answer']}\n\n"
            f"Customer inquiry:\n{state['input_text']}"
        )

        try:
            response = self._llm.invoke([
                SystemMessage(content=_SYSTEM_COMPOSE.format(language=lang_label)),
                HumanMessage(content=prompt),
            ])
            answer = response.content.strip()
        except Exception as exc:
            logger.warning("compose_faq_response LLM failed: %s", exc)
            answer = top["answer"]  # raw FAQ answer as fallback

        return {**state, "faq_answer": answer, "action": "faq_response"}

    # ── Node: create_ticket ──────────────────────────────────────────────────

    def create_ticket(self, state: SupportState) -> SupportState:
        logger.info("Node: create_ticket", extra={"session": state["session_id"]})

        priority = self._derive_priority(
            state["urgency"], state["sentiment"], state["intent"]
        )

        record = TicketRecord(
            subject=state["inquiry_summary"],
            description=state["input_text"],
            intent=state["intent"],
            priority=priority,
            sentiment=state["sentiment"],
            language=state["language"],
            requester_session=state["session_id"],
        )

        result = self._tickets.create_ticket(record)
        return {**state, "ticket_result": result.model_dump(), "action": "ticket_created"}

    # ── Node: finalize ───────────────────────────────────────────────────────

    def finalize(self, state: SupportState) -> SupportState:
        logger.info("Node: finalize", extra={"session": state["session_id"]})

        analysis = {
            "intent": state["intent"],
            "urgency": state["urgency"],
            "sentiment": state["sentiment"],
            "language": state["language"],
            "summary": state["inquiry_summary"],
        }

        if state["action"] == "faq_response":
            top = state["faq_results"][0]
            final: dict[str, Any] = {
                "action": "faq_response",
                "analysis": analysis,
                "faq_match": {
                    "id": top["id"],
                    "category": top["category"],
                    "question": top["question"],
                    "confidence": state["faq_confidence"],
                    "matched_keywords": top["matched_keywords"],
                },
                "answer": state["faq_answer"],
            }
        else:
            final = {
                "action": "ticket_created",
                "analysis": analysis,
                "faq_match": {
                    "confidence": state["faq_confidence"],
                    "matched_keywords": (
                        state["faq_results"][0]["matched_keywords"]
                        if state["faq_results"] else []
                    ),
                },
                "ticket": state["ticket_result"],
            }

        return {**state, "final_result": final, "error": None}

    # ── Routing ──────────────────────────────────────────────────────────────

    @staticmethod
    def _route_after_faq(
        state: SupportState,
    ) -> Literal["compose_faq_response", "create_ticket"]:
        has_match = state["faq_confidence"] >= FAQDatabase.FAQ_ESCALATION_THRESHOLD
        if has_match and not state["forced_escalate"]:
            return "compose_faq_response"
        return "create_ticket"

    # ── Graph Builder ─────────────────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(SupportState)

        graph.add_node("analyze_inquiry", self.analyze_inquiry)
        graph.add_node("search_faq", self.search_faq)
        graph.add_node("compose_faq_response", self.compose_faq_response)
        graph.add_node("create_ticket", self.create_ticket)
        graph.add_node("finalize", self.finalize)

        graph.set_entry_point("analyze_inquiry")
        graph.add_edge("analyze_inquiry", "search_faq")
        graph.add_conditional_edges(
            "search_faq",
            self._route_after_faq,
            {
                "compose_faq_response": "compose_faq_response",
                "create_ticket": "create_ticket",
            },
        )
        graph.add_edge("compose_faq_response", "finalize")
        graph.add_edge("create_ticket", "finalize")
        graph.add_edge("finalize", END)

        return graph.compile()

    def run(self, input_text: str, session_id: str) -> dict[str, Any]:
        initial: SupportState = {
            "input_text": input_text,
            "session_id": session_id,
            "intent": "",
            "urgency": "",
            "sentiment": "",
            "language": "",
            "inquiry_summary": "",
            "faq_results": [],
            "faq_confidence": 0.0,
            "action": "",
            "forced_escalate": False,
            "faq_answer": "",
            "ticket_result": {},
            "final_result": {},
            "error": None,
        }
        final_state = self._graph.invoke(initial)
        return final_state["final_result"]

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _derive_priority(urgency: str, sentiment: str, intent: str) -> TicketPriority:
        if intent == "complaint" or (urgency == "high" and sentiment == "negative"):
            return TicketPriority.CRITICAL
        if urgency == "high":
            return TicketPriority.HIGH
        if urgency == "medium":
            return TicketPriority.MEDIUM
        return TicketPriority.LOW


# ── SupportAgent ───────────────────────────────────────────────────────────────

class SupportAgent(BaseAgent):
    """Öffentliche Agent-Klasse. Delegiert Logik an SupportGraph (LangGraph)."""

    agent_type = "support"

    def __init__(self) -> None:
        super().__init__()
        self._workflow = SupportGraph(
            llm=_build_llm(),
            faq=FAQDatabase(),
            tickets=TicketSystem(),
        )

    def _run(self, request: AgentRequest) -> dict[str, Any]:
        return self._workflow.run(
            input_text=request.text,
            session_id=request.session_id,
        )
