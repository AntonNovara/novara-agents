"""
Onboarding Agent – vollständig implementiert.

Workflow (LangGraph StateGraph):
  parse_customer_data   ← LLM: company_name, contact_name, contact_email,
        ↓                        plan, industry, team_size, primary_use_case, language
  generate_checklist    ← DETERMINISTISCH: build_checklist(plan, industry)
        ↓
  compose_welcome_email ← LLM: personalisierte Willkommens-E-Mail mit
        ↓                        SUBJECT-Zeile, Checklisten-Preview, nächste Schritte
  send_welcome          ← NotificationSystem.send_email()  (Mock → SendGrid / SES)
        ↓
  log_to_tracker        ← OnboardingTracker.create_onboarding()  (Mock → CS-System)
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
from tools.notification_system import NotificationSystem
from tools.onboarding_tracker import OnboardingRecord, OnboardingTracker, build_checklist

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

class OnboardingState(TypedDict):
    input_text: str
    session_id: str

    # set by parse_customer_data
    company_name: str
    contact_name: str
    contact_email: str
    plan: str            # starter | pro | enterprise
    industry: str
    team_size: Optional[int]
    primary_use_case: str
    language: str        # de | en

    # set by generate_checklist
    checklist: list[dict]   # serialised ChecklistItem dicts

    # set by compose_welcome_email
    email_subject: str
    email_body: str

    # set by send_welcome
    notification_result: dict

    # set by log_to_tracker
    onboarding_result: dict

    # final
    final_result: dict
    error: Optional[str]


# ── LLM Prompts ────────────────────────────────────────────────────────────────

_SYSTEM_PARSE = f"""\
Du bist ein Customer-Success-Analyst bei Novara Automation (Wien, Österreich).
Novara hilft Handwerksbetrieben (primär Elektrikerbetriebe), manuelle Prozesse
zu automatisieren.

=== NOVARA WISSENSDATENBANK (Pakete, Onboarding-Phasen, Kundentypen) ===
{_WISSEN}
=== ENDE WISSENSDATENBANK ===

Extrahiere strukturierte Onboarding-Daten aus einer Neukunden-Nachricht, E-Mail oder CRM-Notiz.
Gib AUSSCHLIESSLICH valides JSON zurück:
{{
  "company_name": string,
  "contact_name": string,
  "contact_email": string (oder "unknown@example.com" wenn nicht erwähnt),
  "plan": eines von ["starter","pro","enterprise"],
  "industry": string (z.B. "elektrik", "installation", "malerei", "tischlerei",
                       "handwerk", "dienstleistung", "handel", "other"),
  "team_size": integer oder null,
  "primary_use_case": string (1-2 Sätze was der Kunde automatisieren möchte),
  "language": "de" | "en"
}}

Plan-Mapping gemäß Novara-Paketen aus der Wissensdatenbank:
  starter    = 1 Kernprozess, €990 einmalig, typisch: verpasste Anrufe / Terminbestätigung
  pro        = 2-3 Prozesse, €2.490 einmalig (entspricht "Growth" in der Wissensdatenbank)
  enterprise = Laufende Betreuung, €590/Monat (entspricht "Retainer" in der Wissensdatenbank)
Default: "starter" wenn unklar.
"""

_SYSTEM_WELCOME = f"""\
Du bist Anton, Gründer von Novara Automation, und schreibst eine personalisierte
Willkommens-E-Mail für einen neuen Kunden.

=== NOVARA WISSENSDATENBANK (Ton, Onboarding-Ablauf, Vorlagen) ===
{_WISSEN}
=== ENDE WISSENSDATENBANK ===

Kundendaten werden bereitgestellt. Schreibe auf {{language}}.

WICHTIGE SPRACHREGELN (strikt einhalten):
1. Betreff: max 9 Wörter, warm und spezifisch für Firma/Anwendungsfall
2. Einstieg mit Vornamen des Kontakts
3. Echte Begeisterung — EINEN spezifischen Aspekt ihres Anwendungsfalls referenzieren
4. Genau 3 konkrete erste Schritte aus der Onboarding-Checkliste (im Kontext bereitgestellt)
   Als nummerierte Liste mit fettem Schrittnamen und einem Satz Erklärung
5. Erwähne den Kickoff-Termin (innerhalb 48h nach Auftragsbestätigung laut Wissensdatenbank)
6. Unterschrift: "Freundliche Grüße, Anton" oder "LG, Anton"
7. Max 12 Zeilen Body. Warm, professionell — nicht werbend
8. Österreichisches Deutsch, "Sie"-Form, kein Technik-Jargon
9. Verwende die Willkommensnachricht-Vorlage aus der Wissensdatenbank als Inspiration

Format:
  SUBJECT: <Betreff>
  (Leerzeile)
  <Body>
"""


# ── Graph ──────────────────────────────────────────────────────────────────────

class OnboardingGraph:

    def __init__(self, llm: ChatAnthropic, notifier: NotificationSystem,
                 tracker: OnboardingTracker) -> None:
        self._llm = llm
        self._notifier = notifier
        self._tracker = tracker
        self._graph = self._build_graph()

    # ── Node: parse_customer_data ────────────────────────────────────────────

    def parse_customer_data(self, state: OnboardingState) -> OnboardingState:
        logger.info("Node: parse_customer_data", extra={"session": state["session_id"]})

        defaults: dict[str, Any] = {
            "company_name":    "Unknown Company",
            "contact_name":    "Unknown Contact",
            "contact_email":   "unknown@example.com",
            "plan":            "starter",
            "industry":        "other",
            "team_size":       None,
            "primary_use_case": state["input_text"][:120],
            "language":        "de",
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
                "contact_email":   data.get("contact_email", defaults["contact_email"]),
                "plan":            data.get("plan", defaults["plan"]),
                "industry":        data.get("industry", defaults["industry"]),
                "team_size":       data.get("team_size"),
                "primary_use_case": data.get("primary_use_case", defaults["primary_use_case"]),
                "language":        data.get("language", defaults["language"]),
            }
        except Exception as exc:
            logger.warning("parse_customer_data LLM failed: %s", exc)
            return {**state, **defaults}

    # ── Node: generate_checklist ─────────────────────────────────────────────

    def generate_checklist(self, state: OnboardingState) -> OnboardingState:
        logger.info("Node: generate_checklist", extra={"session": state["session_id"]})
        items = build_checklist(state["plan"], state["industry"])
        return {**state, "checklist": [item.model_dump() for item in items]}

    # ── Node: compose_welcome_email ──────────────────────────────────────────

    def compose_welcome_email(self, state: OnboardingState) -> OnboardingState:
        logger.info("Node: compose_welcome_email", extra={"session": state["session_id"]})

        # Provide the first 5 checklist items as context for the email
        checklist_preview = "\n".join(
            f"  {i+1}. [{item['owner'].upper()}] {item['title']} (Tag {item['due_days']}): {item['description']}"
            for i, item in enumerate(state["checklist"][:5])
        )

        context = (
            f"Company:       {state['company_name']}\n"
            f"Contact:       {state['contact_name']}\n"
            f"Email:         {state['contact_email']}\n"
            f"Plan:          {state['plan']}\n"
            f"Industry:      {state['industry']}\n"
            f"Team size:     {state['team_size'] or 'unknown'}\n"
            f"Use case:      {state['primary_use_case']}\n\n"
            f"First 5 onboarding checklist items:\n{checklist_preview}"
        )

        lang = "German" if state["language"] == "de" else "English"
        fallback_first = state["contact_name"].split()[0]
        fallback_subject = f"Willkommen bei Novara – {state['company_name']}"
        fallback_body = (
            f"Hallo {fallback_first},\n\n"
            f"herzlich willkommen bei Novara Automation! Wir freuen uns, {state['company_name']} "
            f"als neuen Kunden begrüßen zu dürfen.\n\n"
            f"Ihr Novara CS-Team meldet sich in Kürze für den Kick-off-Call."
            "\n\nViele Grüße\nIhr Novara CS-Team"
        )

        try:
            response = self._llm.invoke([
                SystemMessage(content=_SYSTEM_WELCOME.format(language=lang)),
                HumanMessage(content=context),
            ])
            raw = response.content.strip()
        except Exception as exc:
            logger.warning("compose_welcome_email LLM failed: %s", exc)
            return {**state, "email_subject": fallback_subject, "email_body": fallback_body}

        subject, body = fallback_subject, raw
        if raw.upper().startswith("SUBJECT:"):
            lines = raw.split("\n", 2)
            subject = lines[0].split(":", 1)[1].strip()
            body = lines[2].strip() if len(lines) > 2 else raw

        return {**state, "email_subject": subject, "email_body": body}

    # ── Node: send_welcome ───────────────────────────────────────────────────

    def send_welcome(self, state: OnboardingState) -> OnboardingState:
        logger.info("Node: send_welcome", extra={"session": state["session_id"]})
        result = self._notifier.send_email(
            to_email=state["contact_email"],
            to_name=state["contact_name"],
            subject=state["email_subject"],
            body=state["email_body"],
            source_agent="onboarding",
        )
        return {**state, "notification_result": result.model_dump()}

    # ── Node: log_to_tracker ─────────────────────────────────────────────────

    def log_to_tracker(self, state: OnboardingState) -> OnboardingState:
        logger.info("Node: log_to_tracker", extra={"session": state["session_id"]})

        from tools.onboarding_tracker import ChecklistItem
        checklist_items = [ChecklistItem(**item) for item in state["checklist"]]

        record = OnboardingRecord(
            company_name=state["company_name"],
            contact_name=state["contact_name"],
            contact_email=state["contact_email"],
            plan=state["plan"],
            industry=state["industry"],
            team_size=state["team_size"],
            primary_use_case=state["primary_use_case"],
            checklist=checklist_items,
        )
        result = self._tracker.create_onboarding(record)
        return {**state, "onboarding_result": result.model_dump()}

    # ── Node: finalize ───────────────────────────────────────────────────────

    def finalize(self, state: OnboardingState) -> OnboardingState:
        logger.info("Node: finalize", extra={"session": state["session_id"]})

        required_items = [i for i in state["checklist"] if i.get("required")]
        customer_items = [i for i in state["checklist"] if i.get("owner") == "customer"]

        final: dict[str, Any] = {
            "customer": {
                "company":       state["company_name"],
                "contact":       state["contact_name"],
                "email":         state["contact_email"],
                "plan":          state["plan"],
                "industry":      state["industry"],
                "team_size":     state["team_size"],
                "primary_use_case": state["primary_use_case"],
            },
            "onboarding": {
                "id":              state["onboarding_result"].get("onboarding_id"),
                "status":          "initiated",
                "checklist_total": len(state["checklist"]),
                "checklist_required": len(required_items),
                "checklist_customer_owned": len(customer_items),
                "checklist":       state["checklist"],
            },
            "welcome_email": {
                "subject":         state["email_subject"],
                "body":            state["email_body"],
                "notification_id": state["notification_result"].get("notification_id"),
                "delivered":       state["notification_result"].get("success", False),
            },
        }
        return {**state, "final_result": final, "error": None}

    # ── Graph Builder ─────────────────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(OnboardingState)

        graph.add_node("parse_customer_data",  self.parse_customer_data)
        graph.add_node("generate_checklist",   self.generate_checklist)
        graph.add_node("compose_welcome_email", self.compose_welcome_email)
        graph.add_node("send_welcome",         self.send_welcome)
        graph.add_node("log_to_tracker",       self.log_to_tracker)
        graph.add_node("finalize",             self.finalize)

        graph.set_entry_point("parse_customer_data")
        graph.add_edge("parse_customer_data",  "generate_checklist")
        graph.add_edge("generate_checklist",   "compose_welcome_email")
        graph.add_edge("compose_welcome_email", "send_welcome")
        graph.add_edge("send_welcome",         "log_to_tracker")
        graph.add_edge("log_to_tracker",       "finalize")
        graph.add_edge("finalize",             END)

        return graph.compile()

    def run(self, input_text: str, session_id: str) -> dict[str, Any]:
        initial: OnboardingState = {
            "input_text":          input_text,
            "session_id":          session_id,
            "company_name":        "",
            "contact_name":        "",
            "contact_email":       "",
            "plan":                "",
            "industry":            "",
            "team_size":           None,
            "primary_use_case":    "",
            "language":            "de",
            "checklist":           [],
            "email_subject":       "",
            "email_body":          "",
            "notification_result": {},
            "onboarding_result":   {},
            "final_result":        {},
            "error":               None,
        }
        return self._graph.invoke(initial)["final_result"]


# ── OnboardingAgent ───────────────────────────────────────────────────────────

class OnboardingAgent(BaseAgent):
    """Erstellt personalisierte Onboarding-Checklisten und versendet Willkommens-E-Mails."""

    agent_type = "onboarding"

    def __init__(self) -> None:
        super().__init__()
        self._workflow = OnboardingGraph(
            llm=_build_llm(),
            notifier=NotificationSystem(),
            tracker=OnboardingTracker(),
        )

    def _run(self, request: AgentRequest) -> dict[str, Any]:
        return self._workflow.run(
            input_text=request.text,
            session_id=request.session_id,
        )
