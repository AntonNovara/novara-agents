"""
SDR Agent – vollständig implementiert.

Workflow (LangGraph StateGraph):
  analyze_input          ← LLM: strukturierte Firmendaten + ICP-Score extrahieren
        ↓
  search_leads           ← LeadDatabase (Fuzzy-Suche nach Firmenname / Branche)
        │                  Falls kein DB-Treffer: LLM generiert Ziel-Persona
        ↓
  score_lead             ← deterministisch: ICP-Score + Senioritäts-Bonus
        ↓
  _route_after_score
        ├── score ≥ 40 (qualifiziert)
        │         ↓
        │   compose_outreach  ← LLM: hochpersonalisierter E-Mail- oder
        │         ↓                   LinkedIn-Text
        │   write_to_crm     ← CRMIntegrationSDR.upsert_lead()
        │         ↓
        │     finalize
        │
        └── score < 40 (disqualifiziert)
                  ↓
            finalize_disqualified
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Literal, Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from agents.base_agent import AgentRequest, BaseAgent
from core.config import settings
from core.knowledge import load_novara_wissen
from tools.crm_integration import CRMIntegrationSDR, LeadRecord
from tools.lead_database import LeadDatabase, LeadSearchResult, ProspectContact

logger = logging.getLogger(__name__)

QUALIFICATION_THRESHOLD = 40
_SENIORITY_BONUS: dict[str, int] = {
    "c_level": 15,
    "director": 10,
    "manager": 5,
    "ic": 0,
}
_ICP_TIER_THRESHOLDS = {"high": 70, "medium": 40}

# Wissensdatenbank einmalig laden
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
    """Parse JSON from LLM output, stripping markdown code fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return json.loads(text.strip())


# ── Graph State ────────────────────────────────────────────────────────────────

class SDRState(TypedDict):
    input_text: str
    session_id: str

    # set by analyze_input
    company_name: str
    industry: str
    company_size: Optional[int]
    pain_points: list[str]
    icp_score: int          # 0-100, LLM-assessed
    icp_rationale: str
    outreach_channel: str   # "email" | "linkedin"
    language: str           # "de" | "en"

    # set by search_leads
    contacts: list[dict]    # serialized ProspectContact-like dicts
    contact_source: str     # "database" | "generated"

    # set by score_lead
    lead_score: int
    score_rationale: str
    qualified: bool

    # set by compose_outreach
    outreach_text: str
    outreach_subject: str   # empty string for linkedin

    # set by write_to_crm
    crm_result: dict

    # final
    final_result: dict
    error: Optional[str]


# ── LLM Prompts (aus Wissensdatenbank aufgebaut) ───────────────────────────────

_SYSTEM_ANALYZE = f"""\
Du bist ein SDR-Analyst bei Novara Automation.
Analysiere die eingehende Lead-Beschreibung anhand der echten Novara-Wissensdatenbank
und extrahiere strukturierte Daten.

=== NOVARA WISSENSDATENBANK ===
{_WISSEN}
=== ENDE WISSENSDATENBANK ===

Gib AUSSCHLIESSLICH valides JSON zurück (kein erklärender Text):
{{
  "company_name": string,
  "industry": string (z.B. "Elektrikerbetrieb", "Installateur", "Malerbetrieb", "Tischlerei",
                     "Sanitär", "Handwerk allgemein", "Dienstleistung", ...),
  "company_size": integer oder null (Anzahl Mitarbeiter),
  "pain_points": [Liste von Strings — konkrete Automatisierungs-Schmerzpunkte, max 4,
                  bevorzuge Schmerzpunkte aus der Wissensdatenbank],
  "outreach_channel": "email" | "linkedin"  (bevorzuge "email" wenn E-Mail im Input),
  "icp_score": integer 0-100 (ICP-Fit für Novara gemäß Wissensdatenbank),
  "icp_rationale": string (1 Satz Begründung auf Deutsch),
  "language": "de" | "en"  (Sprache des Input-Texts)
}}

ICP-Scoring gemäß Wissensdatenbank:
  SEHR HOCH (85-100): Elektrikerbetrieb Wien, 1-10 MA, Inhaber auf Baustelle,
    Büro läuft nebenher, kein CRM, verpasste Anrufe, manuelle Angebote
  HOCH (70-84): Anderer Handwerksbetrieb Wien/DACH (Installateur, Maler, Tischler, ...),
    ähnliches Profil wie oben
  MITTEL (45-69): KMU Wien/DACH, manuelle Prozesse, Optimierungspotenzial erkennbar
  NIEDRIG (0-44): Bereits professionell digitalisiert, >50 MA, kein Interesse
    an Effizienz, Tech-Start-up (DIY), Non-Profit

WICHTIG: Kleine Betriebe (1-10 MA) in Wien/Österreich mit Inhaber-Profil sind
KEIN Nachteil — das ist exakt Novaras Zielgruppe!
"""

_SYSTEM_GENERATE_PERSONA = f"""\
Du bist ein SDR-Analyst bei Novara Automation.
Kein Kontakt wurde in unserer Datenbank für dieses Unternehmen gefunden.
Generiere basierend auf den Firmendaten den wahrscheinlichsten Ansprechpartner
für einen Handwerksbetrieb / KMU in Österreich.

=== NOVARA ICP AUS WISSENSDATENBANK ===
{_WISSEN}
=== ENDE ===

Für Handwerksbetriebe (Elektriker, Installateur, Maler, etc.) ist der Ansprechpartner
IMMER der Inhaber/Geschäftsführer — nie ein IT-Leiter oder Operations Manager.

Gib AUSSCHLIESSLICH valides JSON zurück:
{{
  "first_name": string (österreichisch-deutscher Vorname),
  "last_name": string (österreichisch-deutscher Nachname passend zur Firma),
  "title": string (bei Handwerksbetrieben: "Inhaber", "Geschäftsführer" oder "Meister"),
  "seniority": "c_level" | "director" | "manager" | "ic"
    (Inhaber/Geschäftsführer = "c_level", bei größeren Betrieben ggf. "director"),
  "email": string (realistisches Format: vorname.nachname@firmen-domain.at),
  "linkedin_url": string (realistisch: linkedin.com/in/vorname-nachname-firma)
}}
"""

_SYSTEM_OUTREACH = f"""\
Du bist ein erfahrener SDR bei Novara Automation und schreibst eine Kalt-Outreach-Nachricht.

=== NOVARA WISSENSDATENBANK (dein Kontext für Ton, Pakete, Einwände, Vorlagen) ===
{_WISSEN}
=== ENDE WISSENSDATENBANK ===

WICHTIGE SPRACHREGELN (strikt einhalten):
1. Einstieg: spezifisch und echt — NIEMALS "Ich hoffe, diese Nachricht findet Sie gut"
2. Nenne 1-2 konkrete Schmerzpunkte des Leads (aus den Top 3 Schmerzen der Wissensdatenbank)
3. Nenne GENAU EINEN konkreten Novara-Anwendungsfall mit echtem Paket-Namen und Preis
   (Starter €990 oder Growth €2.490) — wenn passend
4. Schließe mit EINER einzigen, unverbindlichen Frage — kein "Haben Sie Zeit für einen Anruf?"
5. LinkedIn: max 6 Zeilen. E-Mail: max 5 Sätze Body + Betreff.
6. Ton: neugierig, nicht drängend — wie ein Kollege der eine echte Beobachtung teilt
7. KEIN Technik-Jargon: kein "KI", kein "Make.com", kein "Automatisierungssoftware"
8. Österreichisches Deutsch. Unterschrift: "LG, Anton" oder "Freundliche Grüße, Anton"
9. Nie verteidigen — immer mit einer Frage weiterdrehen

Verwende die Kalt-E-Mail-Vorlage und den Anruf-Ablauf aus der Wissensdatenbank als Vorlage.

Für E-MAIL: erste Zeile muss sein "SUBJECT: <Betreff>" dann Leerzeile dann Body.
Für LINKEDIN: nur der Nachrichtentext, kein Betreff.
Sprache: {{language}}
"""


# ── Graph ──────────────────────────────────────────────────────────────────────

class SDRGraph:
    """LangGraph-Workflow für den SDR Agent."""

    def __init__(self, llm: ChatAnthropic, db: LeadDatabase, crm: CRMIntegrationSDR) -> None:
        self._llm = llm
        self._db = db
        self._crm = crm
        self._graph = self._build_graph()

    # ── Node: analyze_input ──────────────────────────────────────────────────

    def analyze_input(self, state: SDRState) -> SDRState:
        logger.info("Node: analyze_input", extra={"session": state["session_id"]})

        defaults: dict[str, Any] = {
            "company_name": "Unknown Company",
            "industry": "Unknown",
            "company_size": None,
            "pain_points": [],
            "icp_score": 50,
            "icp_rationale": "Could not extract company data",
            "outreach_channel": "linkedin",
            "language": "de",
        }

        try:
            response = self._llm.invoke([
                SystemMessage(content=_SYSTEM_ANALYZE),
                HumanMessage(content=state["input_text"]),
            ])
            data = _parse_llm_json(response.content)
            return {
                **state,
                "company_name": data.get("company_name", defaults["company_name"]),
                "industry": data.get("industry", defaults["industry"]),
                "company_size": data.get("company_size"),
                "pain_points": data.get("pain_points", []),
                "icp_score": int(data.get("icp_score", defaults["icp_score"])),
                "icp_rationale": data.get("icp_rationale", defaults["icp_rationale"]),
                "outreach_channel": data.get("outreach_channel", defaults["outreach_channel"]),
                "language": data.get("language", defaults["language"]),
            }
        except Exception as exc:
            logger.warning("analyze_input LLM failed, using defaults: %s", exc)
            return {**state, **defaults}

    # ── Node: search_leads ───────────────────────────────────────────────────

    def search_leads(self, state: SDRState) -> SDRState:
        logger.info("Node: search_leads", extra={"session": state["session_id"]})

        results: list[LeadSearchResult] = self._db.search(
            company_name=state["company_name"],
            industry=state["industry"],
            top_k=3,
        )

        # Only use a DB contact if it's a direct company-name match.
        # Industry-only matches don't represent the actual target company.
        name_matches = [r for r in results if r.match_reason == "company_name"]
        if name_matches:
            contacts = [self._serialize_contact(r.contact) for r in name_matches[:3]]
            logger.debug("Lead DB hit (company name)", extra={"score": name_matches[0].match_score})
            return {**state, "contacts": contacts, "contact_source": "database"}

        # No company-name match → ask LLM to generate a target persona
        logger.debug("No DB match — generating persona via LLM")
        try:
            prompt = (
                f"Company: {state['company_name']}\n"
                f"Industry: {state['industry']}\n"
                f"Size: {state['company_size'] or 'unknown'} employees\n"
                f"Pain points: {', '.join(state['pain_points'])}"
            )
            response = self._llm.invoke([
                SystemMessage(content=_SYSTEM_GENERATE_PERSONA),
                HumanMessage(content=prompt),
            ])
            persona = _parse_llm_json(response.content)
            contact = {
                "contact_id": f"gen-{uuid.uuid4().hex[:8]}",
                "first_name": persona.get("first_name", "Max"),
                "last_name": persona.get("last_name", "Mustermann"),
                "title": persona.get("title", "Head of Operations"),
                "seniority": persona.get("seniority", "director"),
                "company": state["company_name"],
                "company_size": state["company_size"],
                "industry": state["industry"],
                "email": persona.get("email", ""),
                "linkedin_url": persona.get("linkedin_url", ""),
                "pain_points": state["pain_points"],
                "tech_stack": [],
            }
        except Exception as exc:
            logger.warning("Persona generation failed: %s", exc)
            contact = {
                "contact_id": f"gen-{uuid.uuid4().hex[:8]}",
                "first_name": "N/A", "last_name": "N/A",
                "title": "Head of Operations", "seniority": "director",
                "company": state["company_name"],
                "company_size": state["company_size"],
                "industry": state["industry"],
                "email": "", "linkedin_url": "",
                "pain_points": state["pain_points"],
                "tech_stack": [],
            }

        return {**state, "contacts": [contact], "contact_source": "generated"}

    # ── Node: score_lead ─────────────────────────────────────────────────────

    def score_lead(self, state: SDRState) -> SDRState:
        logger.info("Node: score_lead", extra={"session": state["session_id"]})

        top = state["contacts"][0] if state["contacts"] else {}
        seniority = top.get("seniority", "ic")
        bonus = _SENIORITY_BONUS.get(seniority, 0)
        score = min(100, state["icp_score"] + bonus)
        qualified = score >= QUALIFICATION_THRESHOLD

        tier = "low"
        for label, threshold in _ICP_TIER_THRESHOLDS.items():
            if score >= threshold:
                tier = label
                break

        rationale_parts = [f"ICP {state['icp_score']}/100 — {state['icp_rationale']}"]
        if bonus:
            rationale_parts.append(f"Seniority-Bonus +{bonus} ({seniority})")

        return {
            **state,
            "lead_score": score,
            "score_rationale": "; ".join(rationale_parts),
            "qualified": qualified,
        }

    # ── Node: compose_outreach ───────────────────────────────────────────────

    def compose_outreach(self, state: SDRState) -> SDRState:
        logger.info("Node: compose_outreach", extra={"session": state["session_id"]})

        top = state["contacts"][0]
        contact_name = f"{top['first_name']} {top['last_name']}"
        lang_label = "German" if state["language"] == "de" else "English"
        channel = state["outreach_channel"]

        context = (
            f"Contact: {contact_name}, {top['title']} at {state['company_name']}\n"
            f"Industry: {state['industry']}\n"
            f"Company size: ~{state['company_size'] or 'unknown'} employees\n"
            f"Pain points: {', '.join(state['pain_points']) or 'not specified'}\n"
            f"Channel: {channel}\n"
        )

        try:
            response = self._llm.invoke([
                SystemMessage(content=_SYSTEM_OUTREACH.format(language=lang_label)),
                HumanMessage(content=context),
            ])
            raw = response.content.strip()
        except Exception as exc:
            logger.warning("compose_outreach LLM failed: %s", exc)
            raw = f"Hallo {top['first_name']},\n\nwir bei Novara Automation helfen {state['industry']}-Unternehmen, manuelle Prozesse zu automatisieren.\n\nHat das für Sie Relevanz?\n\nBeste Grüße"

        subject = ""
        body = raw
        if channel == "email" and raw.upper().startswith("SUBJECT:"):
            lines = raw.split("\n", 2)
            subject = lines[0].split(":", 1)[1].strip()
            body = lines[2].strip() if len(lines) > 2 else raw

        return {**state, "outreach_text": body, "outreach_subject": subject}

    # ── Node: write_to_crm ───────────────────────────────────────────────────

    def write_to_crm(self, state: SDRState) -> SDRState:
        logger.info("Node: write_to_crm", extra={"session": state["session_id"]})

        top = state["contacts"][0]
        score = state["lead_score"]
        tier = "high" if score >= 70 else "medium" if score >= 40 else "low"

        record = LeadRecord(
            company_name=state["company_name"],
            contact_name=f"{top['first_name']} {top['last_name']}",
            contact_title=top["title"],
            contact_email=top.get("email") or None,
            contact_linkedin=top.get("linkedin_url") or None,
            industry=state["industry"],
            company_size=state["company_size"],
            lead_score=score,
            icp_tier=tier,
            outreach_channel=state["outreach_channel"],
            outreach_subject=state.get("outreach_subject") or None,
            outreach_text=state["outreach_text"],
            pain_points=state["pain_points"],
            contact_source=state["contact_source"],
        )

        result = self._crm.upsert_lead(record)
        return {**state, "crm_result": result.model_dump()}

    # ── Node: finalize ───────────────────────────────────────────────────────

    def finalize(self, state: SDRState) -> SDRState:
        logger.info("Node: finalize", extra={"session": state["session_id"]})

        top = state["contacts"][0] if state["contacts"] else {}
        final: dict[str, Any] = {
            "qualified": True,
            "company": {
                "name": state["company_name"],
                "industry": state["industry"],
                "size": state["company_size"],
                "pain_points": state["pain_points"],
            },
            "icp": {
                "score": state["icp_score"],
                "rationale": state["icp_rationale"],
            },
            "contact": {
                "name": f"{top.get('first_name', '')} {top.get('last_name', '')}".strip(),
                "title": top.get("title", ""),
                "email": top.get("email", ""),
                "linkedin": top.get("linkedin_url", ""),
                "source": state["contact_source"],
                "seniority": top.get("seniority", ""),
            },
            "lead_score": state["lead_score"],
            "score_rationale": state["score_rationale"],
            "outreach": {
                "channel": state["outreach_channel"],
                "subject": state.get("outreach_subject", ""),
                "message": state["outreach_text"],
            },
            "crm": state["crm_result"],
        }
        return {**state, "final_result": final, "error": None}

    def finalize_disqualified(self, state: SDRState) -> SDRState:
        logger.info("Node: finalize_disqualified", extra={"score": state.get("lead_score")})

        top = state["contacts"][0] if state["contacts"] else {}
        final: dict[str, Any] = {
            "qualified": False,
            "company": {
                "name": state["company_name"],
                "industry": state["industry"],
                "size": state["company_size"],
            },
            "icp": {
                "score": state["icp_score"],
                "rationale": state["icp_rationale"],
            },
            "lead_score": state["lead_score"],
            "score_rationale": state["score_rationale"],
            "message": (
                f"Lead '{state['company_name']}' disqualifiziert "
                f"(Score {state['lead_score']}/{QUALIFICATION_THRESHOLD} min). "
                "Kein CRM-Eintrag, keine Outreach-Nachricht erstellt."
            ),
        }
        return {**state, "final_result": final, "error": None}

    # ── Routing ──────────────────────────────────────────────────────────────

    @staticmethod
    def _route_after_score(
        state: SDRState,
    ) -> Literal["compose_outreach", "finalize_disqualified"]:
        return "compose_outreach" if state["qualified"] else "finalize_disqualified"

    # ── Graph Builder ─────────────────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(SDRState)

        graph.add_node("analyze_input", self.analyze_input)
        graph.add_node("search_leads", self.search_leads)
        graph.add_node("score_lead", self.score_lead)
        graph.add_node("compose_outreach", self.compose_outreach)
        graph.add_node("write_to_crm", self.write_to_crm)
        graph.add_node("finalize", self.finalize)
        graph.add_node("finalize_disqualified", self.finalize_disqualified)

        graph.set_entry_point("analyze_input")
        graph.add_edge("analyze_input", "search_leads")
        graph.add_edge("search_leads", "score_lead")
        graph.add_conditional_edges(
            "score_lead",
            self._route_after_score,
            {
                "compose_outreach": "compose_outreach",
                "finalize_disqualified": "finalize_disqualified",
            },
        )
        graph.add_edge("compose_outreach", "write_to_crm")
        graph.add_edge("write_to_crm", "finalize")
        graph.add_edge("finalize", END)
        graph.add_edge("finalize_disqualified", END)

        return graph.compile()

    def run(self, input_text: str, session_id: str) -> dict[str, Any]:
        initial: SDRState = {
            "input_text": input_text,
            "session_id": session_id,
            "company_name": "",
            "industry": "",
            "company_size": None,
            "pain_points": [],
            "icp_score": 0,
            "icp_rationale": "",
            "outreach_channel": "linkedin",
            "language": "de",
            "contacts": [],
            "contact_source": "",
            "lead_score": 0,
            "score_rationale": "",
            "qualified": False,
            "outreach_text": "",
            "outreach_subject": "",
            "crm_result": {},
            "final_result": {},
            "error": None,
        }
        final_state = self._graph.invoke(initial)
        return final_state["final_result"]

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _serialize_contact(c: ProspectContact) -> dict:
        return {
            "contact_id": c.contact_id,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "title": c.title,
            "seniority": c.seniority,
            "company": c.company,
            "company_size": c.company_size,
            "industry": c.industry,
            "email": c.email,
            "linkedin_url": c.linkedin_url,
            "pain_points": list(c.pain_points),
            "tech_stack": list(c.tech_stack),
        }


# ── SDRAgent ───────────────────────────────────────────────────────────────────

class SDRAgent(BaseAgent):
    """Öffentliche Agent-Klasse. Delegiert Logik an SDRGraph (LangGraph)."""

    agent_type = "sdr"

    def __init__(self) -> None:
        super().__init__()
        self._workflow = SDRGraph(
            llm=_build_llm(),
            db=LeadDatabase(),
            crm=CRMIntegrationSDR(
                endpoint=settings.crm_endpoint,
                api_key=settings.crm_api_key.get_secret_value(),
            ),
        )

    def _run(self, request: AgentRequest) -> dict[str, Any]:
        return self._workflow.run(
            input_text=request.text,
            session_id=request.session_id,
        )
