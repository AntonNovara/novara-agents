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
        │   check_consent      ← core.consent: Opt-out-Prüfung für den gewählten Kanal
        │         ↓
        │   _route_after_consent
        │         ├── erlaubt
        │         │       ↓
        │         │  compose_outreach  ← LLM: hochpersonalisierter E-Mail- oder
        │         │       ↓                   LinkedIn-Text + Pflicht-Offenlegung
        │         │       ↓                   (EU AI Act Art. 50, siehe AI_DISCLOSURE_DE)
        │         │  write_to_crm     ← CRMIntegrationSDR.upsert_lead()
        │         │       ↓
        │         │  schedule_sequence ← tools.sequence_scheduler: meldet den Lead für die
        │         │       ↓                Multi-Touch-Kadenz an (E-Mail/LinkedIn/Anruf, Retries)
        │         │     finalize
        │         │
        │         └── Opt-out hinterlegt
        │                   ↓
        │             finalize_opted_out   (kein Outreach-Text, kein CRM-Eintrag)
        │
        └── score < 40 (disqualifiziert)
                  ↓
            finalize_disqualified

Zweiter, unabhängiger Workflow im selben Modul: InboundChatGraph
(Landing-Page-Chat-Widget, siehe main.py POST /api/v1/chat/landing).
Anders als der Outbound-Flow oben (ein Lead-Text rein, eine Outreach-
Nachricht raus, EIN Aufruf) ist das hier ein mehrstufiges Gespräch
(session_id-basiertes In-Memory-Verlauf, mehrere Turns), das Fragen aus
novara_wissen.txt beantwortet und den ICP-Fit über den GESAMTEN
Gesprächsverlauf einschätzt, nicht nur pro Nachricht. Teilt sich mit dem
Outbound-Flow: die Wissensdatenbank (_WISSEN), die ICP-Scoring-Skala
(identische Schwellwerte/Rubrik), AI_DISCLOSURE_DE und QUALIFICATION_THRESHOLD.
Teilt sich NICHT: LeadDatabase, CRMIntegrationSDR, Consent-Ledger,
Sequence Scheduler — ein Chat-Besucher ist kein Outbound-Lead, es gibt
keinen Kaltakquise-Kanal und daher keinen Opt-out zu prüfen.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from agents.base_agent import AgentRequest, BaseAgent
from agents.guardian_agent import resilient_node
from core import consent, customer_state, lead_capture
from core.config import settings
from core.knowledge import load_novara_wissen
from core.outbound_guard import OPT_OUT_LINE_DE, has_opt_out, review_outreach
from core.llm import build_llm, cached_system_message
from core.security import SecurityLayer
from tools import lead_notifier, sequence_scheduler
from tools.crm_integration import CRMIntegrationSDR, LeadRecord
from tools.document_parser import DocumentParser
from tools import prospect_audit
from tools.lead_database import LeadDatabase, LeadSearchResult, ProspectContact

logger = logging.getLogger(__name__)

QUALIFICATION_THRESHOLD = 40
# Outbound-Kaltakquise: Mindest-ICP (OHNE Seniority-Bonus). Die Rubrik ordnet "anderes
# Handwerk" bei 70-84 ein, beliebige KMU nur bei 45-69 -- Novaras Angebot (verpasste
# Anrufe/WhatsApp/Angebote) passt zum Handwerk, nicht zu jeder Pyme. Kalt angeschriebene
# Nicht-Handwerksbetriebe (z. B. Bäckerei) bekamen sonst einen erfundenen Pitch. Der
# Inbound-Chat behält QUALIFICATION_THRESHOLD: dort hat der Besucher uns selbst kontaktiert.
OUTBOUND_MIN_ICP = 70
_SENIORITY_BONUS: dict[str, int] = {
    "c_level": 15,
    "director": 10,
    "manager": 5,
    "ic": 0,
}
_ICP_TIER_THRESHOLDS = {"high": 70, "medium": 40}

# EU AI Act Art. 50 – Transparenzpflicht (in Kraft seit 2. August 2026): wer
# mit einem KI-System interagiert, muss das erkennen können, sofern es nicht
# offensichtlich ist. Der vom LLM erzeugte Outreach-Text selbst ist NICHT
# vertrauenswürdig genug, um diese Pflicht allein zu erfüllen (siehe
# core/security.py — dieselbe Philosophie wie beim Credential-Hard-Block:
# eine Prompt-Anweisung ist eine Empfehlung an das LLM, deterministischer
# Code ist die Garantie). Die Konstante wird daher sowohl in den
# System-Prompt injiziert (_SYSTEM_OUTREACH) als auch deterministisch in
# compose_outreach() angehängt, falls das LLM sie ausgelassen haben sollte.
# Gleiche Konstante/Formulierung in agents/voice_agent.py — dort bewusst
# dupliziert statt importiert, weil voice_agent.py absichtlich unabhängig
# von agents/sdr_agent.py bleibt (siehe CLAUDE.md, Abschnitt "Voice Agent").
AI_DISCLOSURE_DE = (
    "Hinweis: Diese Nachricht/dieser Anruf wird von einem "
    "KI-System im Auftrag von {client_name} erstellt."
)
_CLIENT_NAME = "Novara Automation"

# Wissensdatenbank einmalig laden
_WISSEN = load_novara_wissen()

# Website des Prospects im Eingabetext: vollständige URL, www.-Adresse oder nackte Domain
# (.at/.com/.de/.wien/.eu). Nicht direkt nach "@"/Wortzeichen, damit E-Mail-Adressen
# ("office@elektro-huber.at") nicht als Website gelten. Bewusst per Regex, nicht per LLM:
# das Ergebnis wird abgerufen -- ein halluzinierter Host wäre ein falscher Audit.
_WEBSITE_RE = re.compile(
    r"(?<![@\w.\-])((?:https?://)?(?:www\.)?[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.(?:at|com|de|wien|eu)\b(?:/[^\s<>\"',)]*)?)",
    re.IGNORECASE,
)


def _find_website(text: str) -> str:
    for m in _WEBSITE_RE.finditer(text or ""):
        url = m.group(1).rstrip(".,;:")
        if url.lower().startswith(("http://", "https://")) or "." in url:
            return url
    return ""


def _audit_context(audit: dict) -> str:
    """Faktenblock für den Outreach-Prompt. Nur verifizierte Ergebnisse, keine Verlustzahlen."""
    gaps = sorted((c for c in audit.get("checks", []) if not c["passed"]), key=lambda c: -c["weight"])[:2]
    if not gaps:
        return ""
    lines = "; ".join(f"{g['label']} ({g['hint'].split(' -- ')[0].split(':')[0]})" for g in gaps)
    return (
        f"Website-Check (automatisch geprüft, {audit['final_url']}, Score {audit['score']}/100). "
        f"Größte Lücken: {lines}.\n"
        "Der Check liest nur den HTML-Quelltext (kein JavaScript) und kann sich irren. Erwähne "
        "HÖCHSTENS EINE dieser Lücken, vorsichtig formuliert (z. B. 'ich konnte auf Ihrer Website "
        "keinen ... finden', NIE 'Ihre Website hat keinen ...'), freundlich, ohne Zahlen zu "
        "Verlusten und ohne weitere Aussagen über die Website.\n"
    )


# Anrede in der ersten Zeile ("Hallo Thomas,", "Sehr geehrter Herr Huber," ...). Nur für
# LLM-erfundene Kontakte (contact_source == "generated") relevant: ein erfundener Name
# darf nie in einer echten Nachricht landen.
_GREETING_WITH_NAME = re.compile(
    r"^\s*(?:hallo|hi|guten\s+tag|servus|liebe[rn]?|sehr\s+geehrte[rn]?)\b[^\n,]*,?", re.IGNORECASE
)


UNKNOWN_CONTACT_NAME = "Unbekannt"


def _contact_display_name(state: "SDRState", top: dict) -> str:
    """Name des Kontakts für CRM/Customer-State/Ergebnis. Bei einem vom LLM erfundenen
    Kontakt (contact_source == "generated") ist der Name geraten -- er darf nie wie ein
    echter Datensatz gespeichert werden."""
    if state.get("contact_source") == "generated":
        return UNKNOWN_CONTACT_NAME
    return f"{top.get('first_name', '')} {top.get('last_name', '')}".strip()


def _neutral_greeting(body: str) -> str:
    """Ersetzt eine Namens-Anrede in der ersten Zeile durch 'Guten Tag,'."""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if _GREETING_WITH_NAME.match(line):
            lines[i] = _GREETING_WITH_NAME.sub("Guten Tag,", line, count=1)
        break
    return "\n".join(lines)


# ── LLM singleton ─────────────────────────────────────────────────────────────

def _extract_balanced_json_object(text: str) -> Optional[str]:
    """
    Findet die erste vollständige, klammer-balancierte '{...}'-Teilzeichenkette
    in `text`, egal wo sie beginnt. Zählt die Klammertiefe manuell statt einer
    gierigen Regex (r"\\{.*\\}") zu vertrauen — die würde bei verschachtelten
    Objekten oder mehreren JSON-Blöcken im selben Text am falschen "}" enden.
    Ignoriert Klammern innerhalb von String-Literalen (inkl. Escape-Sequenzen),
    damit ein reply-Text wie "... die {Firma} ..." die Zählung nicht stört.
    Gibt None zurück, wenn keine öffnende '{' existiert oder die Klammern nie
    wieder auf Tiefe 0 zurückkehren (abgeschnittene Antwort).
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _strip_markdown_fence(text: str) -> str:
    """
    Entfernt eine Markdown-Codefence (```...``` oder ```json...```), falls der
    GESAMTE getrimmte Text von einer eingeschlossen ist -- ohne jede JSON-
    Parse-Pflicht. Fallback-Helfer für respond_and_qualify(), wenn das LLM in
    reinem Fließtext (ggf. in eine Fence verpackt) statt im geforderten JSON
    geantwortet hat; siehe _parse_llm_json()s Docstring für den Normalfall.
    """
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped


def _parse_llm_json(text: str) -> dict:
    """
    Parst ein JSON-Objekt aus einer LLM-Antwort, mehrstufig und robust gegen
    reale Claude-Ausgabeformate, die kein reines JSON sind:

    1. Direkter Versuch (`json.loads` auf den getrimmten Text) — der
       Normalfall, wenn das LLM sich an die Prompt-Vorgabe hält.
    2. Markdown-Codefence IRGENDWO im Text (nicht nur am Anfang) — Claude
       stellt einer ```json-Fence gelegentlich erklärenden Fließtext voran
       ("Hier ist die Analyse:\\n```json\\n{...}\\n```").
    3. Ein balanciertes '{...}'-Objekt irgendwo im Text (siehe
       _extract_balanced_json_object) — deckt reinen Fließtext mit
       eingebettetem JSON ab ("Sicher, hier ist meine Antwort: {...} Lass es
       mich wissen.") und war der eigentliche Auslöser des Bugs: ein LLM, das
       in reinem Text ODER unstrukturiertem Markdown ohne jede Fence
       antwortet, ließ das alte `json.loads(text.strip())` sofort mit
       "Expecting value: line 1 column 1" scheitern, noch bevor überhaupt
       nach einem JSON-Objekt gesucht wurde.

    Wirft ValueError mit einer klaren Meldung (inkl. Text-Ausschnitt), wenn
    sich GAR KEIN JSON-Objekt extrahieren lässt — die Aufrufer (
    respond_and_qualify, analyze_input, search_leads-Persona) fangen das
    jeweils ab und wenden ihren eigenen, kontextpassenden Fallback an (siehe
    deren Except-Blöcke): respond_and_qualify nutzt in diesem Fall den
    Rohtext selbst als Chat-Antwort (_strip_markdown_fence) statt eine
    generische Fehlermeldung zu zeigen, die anderen beiden Nodes fallen auf
    feste Default-Werte zurück.
    """
    stripped = text.strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", stripped, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    obj = _extract_balanced_json_object(stripped)
    if obj is not None:
        try:
            return json.loads(obj)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Kein valides JSON-Objekt in LLM-Antwort gefunden: {stripped[:200]!r}")


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

    # set by check_consent
    consent_allowed: bool
    consent_identifier: str
    consent_reason: str

    # set by audit_prospect (leer, wenn keine Website im Input oder Audit fehlgeschlagen)
    website_url: str
    audit: dict

    # set by compose_outreach
    outreach_text: str
    outreach_subject: str   # empty string for linkedin
    outreach_guard_violations: list[str]  # Verstöße des LLM-Texts (leer = sauber); siehe core/outbound_guard.py

    # set by write_to_crm
    crm_result: dict

    # set by schedule_sequence
    sequence_id: str

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

PFLICHT-OFFENLEGUNG (EU AI Act Art. 50, in Kraft seit 2. August 2026):
Die Nachricht MUSS als allerletzten Satz genau diesen Offenlegungssatz enthalten,
unverändert und unabhängig von der gewählten Sprache:
"{AI_DISCLOSURE_DE.format(client_name=_CLIENT_NAME)}"

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
7. KEIN Technik-Jargon in der eigentlichen Nachricht: kein "KI", kein "Make.com", kein
   "Automatisierungssoftware" (die Pflicht-Offenlegung oben ist davon ausgenommen —
   die MUSS wörtlich "KI-System" enthalten)
8. Österreichisches Deutsch. Unterschrift: "LG, Anton" oder "Freundliche Grüße, Anton"
9. Nie verteidigen — immer mit einer Frage weiterdrehen
10. Letzter Satz der Nachricht = die Pflicht-Offenlegung oben, wörtlich übernommen

Verwende die Kalt-E-Mail-Vorlage und den Anruf-Ablauf aus der Wissensdatenbank als Vorlage.

Für E-MAIL: erste Zeile muss sein "SUBJECT: <Betreff>" dann Leerzeile dann Body.
Für LINKEDIN: nur der Nachrichtentext, kein Betreff.
Sprache: {{language}}
"""


# ── Graph ──────────────────────────────────────────────────────────────────────

class SDRGraph:
    """LangGraph-Workflow für den SDR Agent."""

    def __init__(self, llm: Any, db: LeadDatabase, crm: CRMIntegrationSDR) -> None:
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
                cached_system_message(_SYSTEM_ANALYZE),
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
        # Nur eine E-Mail, die WIRKLICH im Lead-Text steht, ist echt. Die vom LLM geratene
        # Adresse/LinkedIn-URL (vorname.nachname@firma.at) wird verworfen: sie würde als
        # Fakt im CRM, im Consent-Ledger und in der Sequenz landen.
        real_email = SecurityLayer.extract_email(state["input_text"]) or ""
        try:
            prompt = (
                f"Company: {state['company_name']}\n"
                f"Industry: {state['industry']}\n"
                f"Size: {state['company_size'] or 'unknown'} employees\n"
                f"Pain points: {', '.join(state['pain_points'])}"
            )
            response = self._llm.invoke([
                cached_system_message(_SYSTEM_GENERATE_PERSONA),
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
                "email": real_email,
                "linkedin_url": "",
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
                "email": real_email, "linkedin_url": "",
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
        qualified = score >= QUALIFICATION_THRESHOLD and state["icp_score"] >= OUTBOUND_MIN_ICP

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

    # ── Node: check_consent ──────────────────────────────────────────────────

    def check_consent(self, state: SDRState) -> SDRState:
        logger.info("Node: check_consent", extra={"session": state["session_id"]})

        top = state["contacts"][0] if state["contacts"] else {}
        channel = state["outreach_channel"]
        identifier = top.get("email") if channel == "email" else top.get("linkedin_url")

        allowed = consent.is_allowed(identifier, channel)
        reason = (
            "kein Opt-out hinterlegt" if allowed
            else f"Kontakt hat für Kanal '{channel}' widersprochen (Opt-out)"
        )
        if not allowed:
            logger.warning(
                "Outreach durch Opt-out blockiert",
                extra={"session": state["session_id"], "channel": channel, "identifier": identifier},
            )

        return {
            **state,
            "consent_allowed": allowed,
            "consent_identifier": identifier or "",
            "consent_reason": reason,
        }

    # ── Node: audit_prospect ─────────────────────────────────────────────────
    # Nur für qualifizierte, einwilligungsfähige Leads (läuft nach check_consent):
    # keine Webseiten-Abrufe für verworfene Leads. Nie blockierend.

    def audit_prospect(self, state: SDRState) -> SDRState:
        logger.info("Node: audit_prospect", extra={"session": state["session_id"]})
        url = _find_website(state["input_text"])
        if not url:
            return {**state, "website_url": "", "audit": {}}
        try:
            result = prospect_audit.run_audit(url, state["company_name"])
        except Exception as exc:  # run_audit fängt selbst, aber dieser Node darf nie den Lauf abbrechen
            logger.warning("audit_prospect fehlgeschlagen: %s", exc)
            return {**state, "website_url": url, "audit": {}}
        if result.error:
            logger.info("audit_prospect ohne Ergebnis (%s)", result.error)
            return {**state, "website_url": url, "audit": {"error": result.error, "audit_id": result.audit_id}}
        return {**state, "website_url": url, "audit": result.to_dict()}

    # ── Node: compose_outreach ───────────────────────────────────────────────

    def compose_outreach(self, state: SDRState) -> SDRState:
        logger.info("Node: compose_outreach", extra={"session": state["session_id"]})

        top = state["contacts"][0]
        # Kontakt vom LLM erfunden (nicht in der Lead-DB): der Name ist geraten und darf
        # nie in der Nachricht stehen -- neutrale Anrede, auch deterministisch erzwungen.
        generated = state.get("contact_source") == "generated"
        contact_name = f"{top['first_name']} {top['last_name']}"
        lang_label = "German" if state["language"] == "de" else "English"
        channel = state["outreach_channel"]

        if generated:
            contact_line = (
                f"Contact: name unknown ({top['title']}) at {state['company_name']} -- "
                "use NO personal name; greet with 'Guten Tag,' only\n"
            )
        else:
            contact_line = f"Contact: {contact_name}, {top['title']} at {state['company_name']}\n"

        context = (
            contact_line +
            f"Industry: {state['industry']}\n"
            f"Company size: ~{state['company_size'] or 'unknown'} employees\n"
            f"Pain points: {', '.join(state['pain_points']) or 'not specified'}\n"
            f"Channel: {channel}\n"
            + _audit_context(state.get("audit") or {})
        )

        try:
            response = self._llm.invoke([
                cached_system_message(_SYSTEM_OUTREACH.format(language=lang_label)),
                HumanMessage(content=context),
            ])
            raw = response.content.strip()
        except Exception as exc:
            logger.warning("compose_outreach LLM failed: %s", exc)
            greeting = "Guten Tag" if generated else f"Hallo {top['first_name']}"
            raw = f"{greeting},\n\nwir bei Novara Automation helfen {state['industry']}-Unternehmen, manuelle Prozesse zu automatisieren.\n\nHat das für Sie Relevanz?\n\nBeste Grüße"

        subject = ""
        body = raw
        if channel == "email" and raw.upper().startswith("SUBJECT:"):
            lines = raw.split("\n", 2)
            subject = lines[0].split(":", 1)[1].strip()
            body = lines[2].strip() if len(lines) > 2 else raw

        if generated:
            body = _neutral_greeting(body)

        # Deterministische Garantie für die AI-Act-Art.-50-Offenlegung: die
        # Prompt-Instruktion oben ist eine Empfehlung ans LLM, kein Beweis.
        # Nur anhängen, wenn sie nicht schon (wörtlich, vom LLM befolgt) da
        # ist, damit sie nicht doppelt erscheint.
        disclosure = AI_DISCLOSURE_DE.format(client_name=_CLIENT_NAME)
        if not has_opt_out(body):
            # Opt-out deterministisch ergänzen (wie die KI-Offenlegung), bevor geprüft wird.
            body = f"{body}\n\n{OPT_OUT_LINE_DE}"
        if disclosure not in body:
            body = f"{body}\n\n{disclosure}"

        # Outbound-Guard: harte Geschäftsregeln (echte Preise, keine Platzhalter/
        # Garantieversprechen, Opt-out). Verstoß -> sichere Vorlage statt LLM-Text.
        verdict = review_outreach(subject, body)
        if not verdict.ok:
            logger.warning(
                "Outbound-Guard: LLM-Outreach verworfen (%s)", "; ".join(verdict.violations),
                extra={"session": state["session_id"]},
            )
            body = (
                f"{'Guten Tag' if generated else 'Hallo ' + top['first_name']},\n\nwir bei Novara Automation helfen "
                f"{state['industry']}-Betrieben, keine Kundenanfrage mehr zu verpassen.\n\n"
                f"Hat das für Sie Relevanz?\n\nBeste Grüße\n\n{OPT_OUT_LINE_DE}\n\n{disclosure}"
            )
            subject = subject or "Kurze Frage zu Ihren Kundenanfragen"

        return {
            **state, "outreach_text": body, "outreach_subject": subject,
            "outreach_guard_violations": verdict.violations,
        }

    # ── Node: write_to_crm ───────────────────────────────────────────────────
    # TODO: icp_tier-Berechnung ist dupliziert mit score_lead(), sollte
    # zentralisiert werden — siehe Session vom 20.07.2026.

    def write_to_crm(self, state: SDRState) -> SDRState:
        logger.info("Node: write_to_crm", extra={"session": state["session_id"]})

        top = state["contacts"][0]
        score = state["lead_score"]
        tier = "high" if score >= 70 else "medium" if score >= 40 else "low"

        record = LeadRecord(
            company_name=state["company_name"],
            contact_name=_contact_display_name(state, top),
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

        customer_state.update_stage(
            "sdr",
            {
                "lead_score": score,
                "icp_tier": tier,
                "industry": state["industry"],
                "pain_points": state["pain_points"],
                "outreach_channel": state["outreach_channel"],
                "contact_name": record.contact_name,
            },
            email=top.get("email") or None,
            company_name=state["company_name"],
            agent_session_id=state["session_id"],
        )

        return {**state, "crm_result": result.model_dump()}

    # ── Node: schedule_sequence ──────────────────────────────────────────────

    def schedule_sequence(self, state: SDRState) -> SDRState:
        logger.info("Node: schedule_sequence", extra={"session": state["session_id"]})

        top = state["contacts"][0]
        identifiers = {
            "email": top.get("email") or None,
            "linkedin": top.get("linkedin_url") or None,
            # ProspectContact/LeadRecord erfassen aktuell keine Telefonnummer
            # (siehe tools/crm_integration.py, _lead_record_to_sheet_row) --
            # der "voice"-Schritt bleibt dadurch immer "skipped", bis das
            # Datenmodell eine Nummer erfasst. Kein automatischer Dialer
            # existiert ohnehin (agents/voice_agent.py ist inbound-only).
            "voice": None,
        }
        crm_result = state.get("crm_result", {})
        seq = sequence_scheduler.enroll(
            lead_key=state["company_name"],
            identifiers=identifiers,
            first_channel=state["outreach_channel"],
            first_success=bool(crm_result.get("success")),
            first_reason=crm_result.get("message", ""),
        )
        return {**state, "sequence_id": seq.sequence_id}

    # ── Node: finalize ───────────────────────────────────────────────────────

    def finalize(self, state: SDRState) -> SDRState:
        logger.info("Node: finalize", extra={"session": state["session_id"]})

        top = state["contacts"][0] if state["contacts"] else {}
        sequence = sequence_scheduler.get(state.get("sequence_id", ""))
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
                "name": _contact_display_name(state, top),
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
                "guard_violations": state.get("outreach_guard_violations", []),
            },
            "website_audit": (
                {"url": state.get("website_url", ""), "score": state["audit"].get("score"),
                 "audit_id": state["audit"].get("audit_id"), "error": state["audit"].get("error", "")}
                if state.get("audit") else {}
            ),
            "crm": state["crm_result"],
            "sequence": {
                "sequence_id": sequence.sequence_id,
                "status": sequence.status,
                "steps": [
                    {
                        "channel": s.channel,
                        "day_offset": s.day_offset,
                        "status": s.status,
                        "attempts": s.attempts,
                        "max_retries": s.max_retries,
                        "last_reason": s.last_reason,
                    }
                    for s in sequence.steps
                ],
            } if sequence else {},
        }
        return {**state, "final_result": final, "error": None}

    def finalize_opted_out(self, state: SDRState) -> SDRState:
        logger.info("Node: finalize_opted_out", extra={"session": state["session_id"]})

        final: dict[str, Any] = {
            "qualified": True,
            "consent_blocked": True,
            "company": {
                "name": state["company_name"],
                "industry": state["industry"],
                "size": state["company_size"],
            },
            "lead_score": state["lead_score"],
            "score_rationale": state["score_rationale"],
            "consent": {
                "channel": state["outreach_channel"],
                "identifier": state["consent_identifier"],
                "reason": state["consent_reason"],
            },
            "message": (
                f"Lead '{state['company_name']}' ist qualifiziert (Score {state['lead_score']}), "
                f"aber für Kanal '{state['outreach_channel']}' liegt ein Opt-out vor. "
                "Kein Outreach-Text erstellt, kein CRM-Eintrag."
            ),
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
                f"(Score {state['lead_score']}, min. {QUALIFICATION_THRESHOLD}; ICP {state['icp_score']}, "
                f"min. {OUTBOUND_MIN_ICP} für Outbound). "
                "Kein CRM-Eintrag, keine Outreach-Nachricht erstellt."
            ),
        }
        return {**state, "final_result": final, "error": None}

    # ── Routing ──────────────────────────────────────────────────────────────

    @staticmethod
    def _route_after_score(
        state: SDRState,
    ) -> Literal["compose_outreach", "finalize_disqualified"]:
        # Rückgabewert "compose_outreach" führt im Graph zu "check_consent",
        # nicht direkt zu "compose_outreach" (siehe _build_graph) — die
        # Consent-Prüfung sitzt zwischen Qualifikation und Nachrichten-
        # erstellung. Name unverändert gelassen, um den bestehenden Test
        # (test_sdr_routing) und die bestehende Literal-Signatur nicht
        # anzufassen.
        return "compose_outreach" if state["qualified"] else "finalize_disqualified"

    @staticmethod
    def _route_after_consent(
        state: SDRState,
    ) -> Literal["compose_outreach", "finalize_opted_out"]:
        return "compose_outreach" if state["consent_allowed"] else "finalize_opted_out"

    # ── Graph Builder ─────────────────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(SDRState)

        graph.add_node("analyze_input", self.analyze_input)
        graph.add_node("search_leads", self.search_leads)
        graph.add_node("score_lead", self.score_lead)
        graph.add_node("check_consent", self.check_consent)
        graph.add_node("audit_prospect", self.audit_prospect)
        graph.add_node("compose_outreach", self.compose_outreach)
        graph.add_node("write_to_crm", self.write_to_crm)
        graph.add_node("schedule_sequence", self.schedule_sequence)
        graph.add_node("finalize", self.finalize)
        graph.add_node("finalize_disqualified", self.finalize_disqualified)
        graph.add_node("finalize_opted_out", self.finalize_opted_out)

        graph.set_entry_point("analyze_input")
        graph.add_edge("analyze_input", "search_leads")
        graph.add_edge("search_leads", "score_lead")
        graph.add_conditional_edges(
            "score_lead",
            self._route_after_score,
            {
                # qualifiziert → erst Consent prüfen, nicht direkt Outreach schreiben
                "compose_outreach": "check_consent",
                "finalize_disqualified": "finalize_disqualified",
            },
        )
        graph.add_conditional_edges(
            "check_consent",
            self._route_after_consent,
            {
                "compose_outreach": "audit_prospect",
                "finalize_opted_out": "finalize_opted_out",
            },
        )
        graph.add_edge("audit_prospect", "compose_outreach")
        graph.add_edge("compose_outreach", "write_to_crm")
        graph.add_edge("write_to_crm", "schedule_sequence")
        graph.add_edge("schedule_sequence", "finalize")
        graph.add_edge("finalize", END)
        graph.add_edge("finalize_disqualified", END)
        graph.add_edge("finalize_opted_out", END)

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
            "consent_allowed": True,
            "consent_identifier": "",
            "consent_reason": "",
            "website_url": "",
            "audit": {},
            "outreach_text": "",
            "outreach_subject": "",
            "crm_result": {},
            "sequence_id": "",
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


# ── Inbound Chat: Session Store ─────────────────────────────────────────────
# In-Memory-Prozess-Singleton, gleiches Muster wie core/consent.py._ledger --
# geht bei Neustart verloren. ANDERS als die anderen In-Memory-Stores in
# diesem Repo ist der Aufrufer hier ein ANONYMER, UNAUTHENTIFIZIERTER
# Website-Besucher (main.py's /api/v1/chat/landing hat bewusst KEINEN
# API-Key-Schutz, siehe dortiger Endpoint-Docstring) -- session_id kommt vom
# Client (Widget), ein böswilliger Akteur könnte beliebig viele erfinden.
# _MAX_INBOUND_SESSIONS + die Verdrängung der ältesten Session in
# _save_inbound_session() sind ein einfaches Not-Ventil dagegen, kein echtes
# Rate-Limiting (siehe CLAUDE.md, "Bekannte Einschränkungen").

_MAX_INBOUND_SESSIONS = 5_000
_MAX_INBOUND_HISTORY_TURNS = 12  # letzte 12 Nachrichten (6 Runden) je Session


class InboundChatSession(BaseModel):
    """Persistierter Zustand EINER Landing-Page-Chat-Session zwischen Turns."""

    session_id: str
    history: list[dict[str, str]] = Field(default_factory=list)  # [{"role": "user"|"assistant", "content": ...}]
    icp_score: int = 0
    icp_rationale: str = ""
    company_name: str = ""
    industry: str = ""
    pain_points: list[str] = Field(default_factory=list)
    contact_name: str = ""  # LLM-extrahierter Besuchername, siehe _SYSTEM_INBOUND_CHAT + core/lead_capture.py
    turn_count: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


_inbound_sessions: dict[str, InboundChatSession] = {}


def _get_inbound_session(session_id: str) -> InboundChatSession:
    return _inbound_sessions.get(session_id) or InboundChatSession(session_id=session_id)


def _save_inbound_session(session: InboundChatSession) -> None:
    session.updated_at = datetime.now(timezone.utc).isoformat()
    if session.session_id not in _inbound_sessions and len(_inbound_sessions) >= _MAX_INBOUND_SESSIONS:
        oldest_id = min(_inbound_sessions, key=lambda sid: _inbound_sessions[sid].updated_at)
        del _inbound_sessions[oldest_id]
    _inbound_sessions[session.session_id] = session


# ── Inbound Chat: Prompt ─────────────────────────────────────────────────────

_SYSTEM_INBOUND_CHAT = f"""\
Du bist ein erfahrener SDR (Sales Development Representative) von Novara
Automation und führst den Chat auf der Novara-Automation-Landing-Page. Ein
Website-Besucher chattet direkt mit dir (Inbound, keine Kaltakquise) — deine
Aufgabe ist nicht nur Fragen beantworten, sondern aktiv, aber nie aufdringlich,
Richtung qualifiziertem Termin verkaufen.

=== NOVARA WISSENSDATENBANK (deine EINZIGE Quelle für Fakten) ===
{_WISSEN}
=== ENDE WISSENSDATENBANK ===

DEINE AUFGABE (mehrere Dinge gleichzeitig, in JEDER Antwort):
1. Beantworte die Frage des Besuchers hilfreich, konkret und im Ton eines
   Kollegen — NUR mit Fakten aus der Wissensdatenbank oben (Pakete, Preise,
   Prozess, Zielgruppe). Erfinde NIEMALS ein Feature, einen Preis oder eine
   Zusage, die nicht in der Wissensdatenbank steht — sag im Zweifel lieber,
   dass du das gern im persönlichen Gespräch klärst, statt zu raten.
2. Schätze im Hintergrund den ICP-Fit des Besuchers anhand des GESAMTEN
   bisherigen Gesprächsverlaufs ein (nicht nur der letzten Nachricht) —
   Firma, Branche, Größe, Schmerzpunkte, alles was bisher gesagt wurde.
3. Behandle Einwände proaktiv nach den Regeln unten und qualifiziere subtil,
   statt nur zu reagieren.
4. Dein Endziel in JEDEM Gespräch: den Besucher zu einem Termin über den
   Buchungslink (DEMO_BOOKING_URL) zu führen, sobald er dafür bereit wirkt.

EINWANDBEHANDLUNG (immer in der Sprache des Besuchers, aber inhaltlich exakt so):
- Einwand "zu teuer" / Preis zu hoch: Lenke IMMER auf ROI, eingesparte Zeit
  und den Charakter als Investition statt Ausgabe — z. B. wie viele Stunden
  manuelle Arbeit oder verpasste Anrufe/Aufträge das Paket im Monat wettmacht.
  Nenne die Zahlen NUR aus der Wissensdatenbank, erfinde keine ROI-Werte.
- Einwand "KI ist zu kompliziert" / keine technischen Kenntnisse: Betone
  ausdrücklich, dass Novara "Done-for-you" ist — zu 100% von uns umgesetzt,
  der Kunde braucht KEINERLEI IT-Kenntnisse, es entsteht kein zusätzlicher
  Aufwand für sein Team.
- Andere Einwände (Zeitpunkt, Vertrauen, "muss das intern abstimmen", ...):
  ernst nehmen, kurz einordnen, dann sanft zur nächsten Qualifizierungsfrage
  oder zum Terminvorschlag überleiten — nie einfach stehen lassen.

SUBTILE QUALIFIZIERUNG: Bevor du einen Termin anbietest, versuche im
natürlichen Gesprächsfluss (nicht als Verhör, nicht in der allerersten
Antwort) herauszufinden: die ungefähre Firmengröße/Mitarbeiterzahl ODER den
größten aktuellen Engpass/Schmerzpunkt des Besuchers (z. B. verpasste
Anrufe, manuelle Angebote, keine Zeit für Admin). Eine dieser beiden
Informationen reicht, um danach glaubwürdig einen Termin vorzuschlagen.
Dräng NICHT auf Firmendaten als allererste Reaktion auf eine reine
Informationsanfrage — beantworte zuerst die eigentliche Frage, aber nutze
danach eine natürliche Gelegenheit für genau eine kurze Rückfrage.

ICP-Scoring gemäß Wissensdatenbank (identische Skala wie im Outbound-SDR):
  SEHR HOCH (85-100): Elektrikerbetrieb Wien, 1-10 MA, Inhaber auf Baustelle,
    Büro läuft nebenher, kein CRM, verpasste Anrufe, manuelle Angebote
  HOCH (70-84): Anderer Handwerksbetrieb Wien/DACH (Installateur, Maler, Tischler, ...),
    ähnliches Profil wie oben
  MITTEL (45-69): KMU Wien/DACH, manuelle Prozesse, Optimierungspotenzial erkennbar
  NIEDRIG (0-44): Bereits professionell digitalisiert, >50 MA, kein Interesse
    an Effizienz, Tech-Start-up (DIY), Non-Profit — ODER schlicht noch zu
    wenig Information bekannt, um sinnvoll einzuschätzen

WICHTIG: Wenn der Besucher noch keine Firmendaten preisgegeben hat, ist ein
niedriger Score korrekt (nicht raten!) — 0 ist der richtige Default bei
einer reinen Informationsanfrage ohne jeden Firmenbezug.

Sobald der ICP-Fit erkennbar hoch genug ist (siehe Skala oben) UND der
Besucher mindestens eine Qualifizierungsinfo genannt hat, biete den Termin
aktiv an — z. B. "Das klingt nach einem guten Fit, am schnellsten klären wir
das in einem kurzen Erstgespräch, wollen wir das gleich einplanen?" statt nur
zu warten, bis der Besucher selbst danach fragt.

Wenn der Besucher von sich aus Kontaktdaten nennt (Name, Telefonnummer,
E-Mail, Firma), bedanke dich kurz dafür und nutze sie natürlich weiter im
Gespräch — erfinde nie einen Namen oder eine Adresse, die nicht genannt wurde.

Gib AUSSCHLIESSLICH valides JSON zurück (kein Text davor/danach):
{{
  "reply": string (deine Chat-Antwort an den Besucher, in der Sprache des Besuchers,
                   2-5 Sätze, kein Technik-Jargon wie "LangGraph" oder "Agent"),
  "company_name": string (bereits bekannter oder neu genannter Firmenname, sonst ""),
  "industry": string (z.B. "Elektrikerbetrieb", "Installateur", "Malerbetrieb", ..., sonst ""),
  "company_size": integer oder null,
  "pain_points": [Liste von Strings, max 4, sonst leere Liste],
  "contact_name": string (Vor-/Nachname des Besuchers, NUR wenn er ihn im
                   Gespräch tatsächlich genannt hat, sonst ""),
  "icp_score": integer 0-100 (Gesamteinschätzung über das GANZE Gespräch, nicht nur diese Nachricht),
  "icp_rationale": string (1 Satz Begründung auf Deutsch),
  "language": "de" | "en"  (Sprache DIESER Besucher-Nachricht)
}}
"""


# ── Inbound Chat: Graph State ────────────────────────────────────────────────

class InboundChatState(TypedDict):
    session_id: str
    message: str                    # DLP-bereinigte Besucher-Nachricht (main.py prüft VOR diesem Aufruf)
    visitor_info: dict[str, Any]    # optionale Formulardaten: name/email/company/phone
    attachment: Optional[dict[str, Any]]  # optional: {"filename", "mime_type", "content_base64"}, siehe main.py LandingAttachment

    # aus der Session geladen, VOR diesem Turn
    history: list[dict[str, str]]
    is_first_turn: bool
    turn_count: int
    created_at: str

    # von receptionist_node gesetzt/aktualisiert
    reply_text: str
    icp_score: int
    icp_rationale: str
    company_name: str
    industry: str
    pain_points: list[str]
    contact_name: str        # LLM-extrahierter Besuchername, siehe core/lead_capture.py
    language: str

    # von document_node gesetzt
    document_summary: str    # extrahierter/beschriebener Anhaltsinhalt, sonst ""
    attachment_error: str    # nutzerverständliche Fehlermeldung bei fehlgeschlagener Extraktion, sonst ""

    # von appointment_node gesetzt
    qualified: bool
    booking_url: Optional[str]

    final_result: dict[str, Any]


# ── Inbound Chat: Anhang-Extraktion (document_node) ─────────────────────────────

# 8 MB deckt ein mehrseitiges PDF-Angebot oder ein Handy-Foto großzügig ab,
# begrenzt aber Missbrauch auf diesem öffentlichen/unauthentifizierten
# Endpoint (siehe main.py landing_chat()-Docstring zum Bedrohungsmodell).
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
# Deckelt, wie viel PDF-Text in den Chat-Kontext/die Lead-Mail einfließt --
# ein 30-seitiges Angebot soll weder das Token-Budget noch die
# Benachrichtigungsmail sprengen.
_MAX_DOCUMENT_TEXT_CHARS = 4_000

_SYSTEM_DOCUMENT_IMAGE = """\
Du analysierst ein Foto oder einen Screenshot, das ein Website-Besucher im
Chat mit Novara Automation hochgeladen hat (typisch: Foto einer Baustelle,
eines handschriftlichen Angebots, einer Excel-Planzeile). Beschreibe in 2-3
knappen Sätzen auf Deutsch, was zu sehen ist und welche für ein
Automatisierungs-Erstgespräch relevanten Details erkennbar sind (Zahlen,
Mengen, Zustand, Firma, ...). Antworte in reinem Fließtext, KEIN JSON.
Erfinde nichts, das nicht erkennbar ist.
"""


def _build_defensive_final_result(
    state: InboundChatState, qualified: bool, tier: str, reply: str
) -> dict[str, Any]:
    """
    Baut final_result robust gegen unerwartete Typen in vorgelagerten
    State-Feldern — jedes Feld einzeln validiert/gecastet statt eines
    einzigen dict-Literals, das bei EINEM kaputten Feld (z. B. pain_points
    als String statt Liste, weil ein Node aus irgendeinem Grund einen
    unerwarteten Wert hinterlassen hat) komplett fehlschlagen würde. Letzte
    Verteidigungslinie vor main.py landing_chat(), das dieses Ergebnis 1:1
    in LandingChatResponse einsetzt (Pydantic validiert dort zusätzlich,
    aber ein sauberes dict hier ist die erste Linie). Gleiche Philosophie
    wie core/security.py SecurityLayer.sanitize_dict(): die letzte Instanz
    vor dem Verlassen des Prozesses darf sich nicht blind auf vorgelagerte
    Nodes verlassen.
    """
    def _safe_str(value: Any, default: str = "") -> str:
        return value if isinstance(value, str) else default

    def _safe_list(value: Any) -> list:
        return value if isinstance(value, list) else []

    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return {
        "reply": _safe_str(reply),
        "qualified": bool(qualified),
        "should_book_demo": bool(qualified),
        "booking_url": state.get("booking_url") if qualified else None,
        "icp": {
            "score": _safe_int(state.get("icp_score"), 0),
            "tier": _safe_str(tier, "low"),
            "rationale": _safe_str(state.get("icp_rationale")),
        },
        "company": {
            "name": _safe_str(state.get("company_name")),
            "industry": _safe_str(state.get("industry")),
            "pain_points": _safe_list(state.get("pain_points")),
        },
        "language": _safe_str(state.get("language"), "de"),
    }


_INBOUND_CHAT_FALLBACK_REPLY = (
    "Entschuldigung, da ist gerade technisch etwas schiefgelaufen — "
    "magst du deine Frage nochmal stellen?"
)


def _inbound_chat_fallback(state: dict[str, Any]) -> dict[str, Any]:
    """
    `fallback_builder` für `@resilient_node` (agents/guardian_agent.py) auf
    allen 4 InboundChatGraph-Nodes -- greift NUR, wenn ein Node trotz
    Retries endgültig fehlschlägt (siehe resilient_node()-Docstring, "Self-
    Healing Middleware"). Reiner State-Passthrough (die Alternative ohne
    fallback_builder) würde bei einem Ausfall von appointment_node oder
    supervisor_node ein LEERES final_result durchreichen -- run() gäbe dann
    `{}` an main.py landing_chat() zurück, was zwar keine Exception ist,
    aber eine leere Antwort an den Besucher. Diese Funktion garantiert
    stattdessen IMMER ein gültiges, nicht-leeres final_result.

    Nutzt state.get("reply_text") weiter, falls ein früherer Node (z. B.
    receptionist_node) bereits erfolgreich eine Antwort erzeugt hatte, bevor
    ein SPÄTERER Node (z. B. supervisor_node) ausfiel -- der Besucher
    bekommt dann trotzdem die echte Antwort, nur ohne die Zusatzschritte
    (Offenlegung/Anhang-Hinweis/Termin-Entscheidung) des ausgefallenen Nodes.
    """
    if state.get("final_result"):
        return state

    reply = state.get("reply_text") or _INBOUND_CHAT_FALLBACK_REPLY
    tier = "low"
    for label, threshold in _ICP_TIER_THRESHOLDS.items():
        if state.get("icp_score", 0) >= threshold:
            tier = label
            break

    final_result = _build_defensive_final_result(state, qualified=False, tier=tier, reply=reply)
    return {**state, "reply_text": reply, "final_result": final_result}


# ── Inbound Chat: Graph ───────────────────────────────────────────────────────

class InboundChatGraph:
    """
    LangGraph-Workflow für das Landing-Page-Chat-Widget (Inbound-SDR).

    4-Node-Architektur (Refactor 17.09.2026, siehe CLAUDE.md):

        receptionist_node ──[hat Anhang?]──┬── ja  → document_node ──┐
                                            └── nein ─────────────────┼→ appointment_node → supervisor_node → END

    1. receptionist_node — Begrüßung/Antwort auf Deutsch oder Englisch,
       ICP-Qualifizierung, Intent (bisher respond_and_qualify()).
    2. document_node — Extraktion aus einem optionalen Anhang (PDF-Angebot,
       Planungs-Tabelle, Foto einer Baustelle). Nur erreicht, wenn ein
       Anhang mitgeschickt wurde (main.py LandingAttachment).
    3. appointment_node — entscheidet, ob JETZT aktiv der Termin-Link
       (settings.demo_booking_url) angeboten wird (bisher Teil von finalize()).
    4. supervisor_node — EU-AI-Act-Offenlegung, Anhang-Hinweis in die Antwort
       einweben, Session-Persistenz, Lead-Capture/-Benachrichtigung,
       defensive JSON-Serialisierung (bisher apply_disclosure() + Rest von
       finalize()).
    """

    def __init__(self, llm: Any) -> None:
        self._llm = llm
        self._graph = self._build_graph()

    # ── Node 1/4: receptionist_node ──────────────────────────────────────────

    @resilient_node(fallback_builder=_inbound_chat_fallback)
    def receptionist_node(self, state: InboundChatState) -> InboundChatState:
        logger.info("Node: receptionist_node", extra={"session": state["session_id"]})

        messages: list = [cached_system_message(_SYSTEM_INBOUND_CHAT)]
        for turn in state["history"]:
            msg_cls = HumanMessage if turn.get("role") == "user" else AIMessage
            messages.append(msg_cls(content=turn.get("content", "")))

        visitor_note = ""
        known = {k: v for k, v in (state.get("visitor_info") or {}).items() if v}
        if known:
            visitor_note = f"[Bekannte Besucherdaten aus einem Formularfeld: {json.dumps(known, ensure_ascii=False)}]\n"
        messages.append(HumanMessage(content=f"{visitor_note}{state['message']}"))

        fallback_reply = (
            "Entschuldigung, da ist gerade technisch etwas schiefgelaufen — "
            "magst du deine Frage nochmal stellen?"
        )

        # Zwei getrennte try/except-Stufen, bewusst NICHT eine gemeinsame:
        # unterschiedliche Fehlerarten verdienen unterschiedliche Fallbacks.
        try:
            response = self._llm.invoke(messages)
        except Exception as exc:
            # Der LLM-AUFRUF selbst ist fehlgeschlagen (Netzwerk, Rate-Limit,
            # Anthropic-Fehler, ...) -- es gibt keinen Antworttext, der sich
            # retten ließe. Nur hier ist die generische Entschuldigung die
            # einzig ehrliche Antwort.
            logger.warning("receptionist_node: LLM-Aufruf fehlgeschlagen: %s", exc)
            return {**state, "reply_text": fallback_reply}

        try:
            data = _parse_llm_json(response.content)
        except Exception as exc:
            # Das LLM HAT geantwortet, aber nicht im geforderten JSON-Format
            # -- z. B. reiner Fließtext oder unstrukturiertes Markdown statt
            # {"reply": ..., "icp_score": ..., ...}. Das war der eigentliche
            # Bug-Report: "Expecting value: line 1 column 1" ist
            # json.loads()s Fehlermeldung für "Text beginnt nicht mit einem
            # gültigen JSON-Token", z. B. wenn Claude direkt in Prosa
            # antwortet statt im Prompt-vorgegebenen JSON. _parse_llm_json()
            # hat bereits mehrere Extraktionsstufen versucht (Codefence
            # irgendwo im Text, balanciertes {...}-Objekt irgendwo im Text --
            # siehe deren Docstring); schlägt selbst DAS fehl, ist der
            # Rohtext der Antwort trotzdem die beste verfügbare Information
            # für den Besucher -- ihn wegzuwerfen und stattdessen die
            # generische Entschuldigung zu zeigen, wäre schlechter als
            # reiner Text ohne ICP-Zusatzdaten für DIESEN Turn. Alle
            # übrigen Felder bleiben unverändert (monotonic, kein
            # Rückschritt ggü. dem bisherigen Sessionstand).
            logger.warning(
                "receptionist_node: LLM-Antwort war kein valides JSON, nutze Rohtext als Antwort: %s",
                exc,
            )
            raw_reply = _strip_markdown_fence(response.content) if isinstance(response.content, str) else ""
            return {**state, "reply_text": raw_reply or fallback_reply}

        new_score = int(data.get("icp_score", state["icp_score"]))
        return {
            **state,
            "reply_text": data.get("reply") or fallback_reply,
            "company_name": data.get("company_name") or state["company_name"],
            "industry": data.get("industry") or state["industry"],
            "pain_points": data.get("pain_points") or state["pain_points"],
            "contact_name": data.get("contact_name") or state.get("contact_name", ""),
            # Monotonic: ein einmal erkannter ICP-Fit soll nicht durch
            # LLM-Rauschen in einer späteren Antwort wieder sinken --
            # sonst könnte should_book_demo mitten im Gespräch flackern.
            "icp_score": max(state["icp_score"], new_score),
            "icp_rationale": data.get("icp_rationale") or state["icp_rationale"],
            "language": data.get("language") or state["language"],
        }

    # ── Node 2/4: document_node ──────────────────────────────────────────────

    @resilient_node(fallback_builder=_inbound_chat_fallback)
    def document_node(self, state: InboundChatState) -> InboundChatState:
        """
        Extraktion aus einem optionalen Anhang (PDF-Angebot, Planungs-
        Tabelle, Foto einer Baustelle). Nur erreicht, wenn
        state["attachment"] gesetzt ist (siehe _route_after_receptionist()).

        JEDER Fehlerpfad (kaputtes Base64, zu große Datei, nicht
        unterstützter Dateityp, kaputtes PDF, LLM-Fehler bei der
        Bildbeschreibung) setzt attachment_error statt eine Exception zu
        werfen -- ein fehlgeschlagener Anhang darf die Konversation nie
        unterbrechen, gleiche Philosophie wie receptionist_node()s
        zweistufiges try/except.
        """
        logger.info("Node: document_node", extra={"session": state["session_id"]})
        attachment = state.get("attachment")
        if not attachment:
            # Sauberer No-op statt "Dateityp '' wird nicht unterstützt" --
            # _route_after_receptionist() leitet zwar nie ohne Anhang
            # hierher, aber der Node selbst soll trotzdem defensiv bleiben,
            # falls er je direkt (z. B. in Tests) ohne Anhang aufgerufen wird.
            return state

        filename = attachment.get("filename") or "Anhang"
        mime_type = (attachment.get("mime_type") or "").lower().strip()
        content_b64 = attachment.get("content_base64", "")

        try:
            raw_bytes = base64.b64decode(content_b64, validate=True)
        except Exception as exc:
            logger.warning("document_node: Base64-Dekodierung fehlgeschlagen: %s", exc)
            return {**state, "attachment_error": "Die Datei konnte nicht gelesen werden."}

        if len(raw_bytes) > _MAX_ATTACHMENT_BYTES:
            logger.warning("document_node: Anhang zu groß", extra={"bytes": len(raw_bytes)})
            return {**state, "attachment_error": "Die Datei ist zu groß (max. 8 MB)."}

        if mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
            try:
                text = DocumentParser().extract_text_from_pdf(raw_bytes)
            except Exception as exc:
                logger.warning("document_node: PDF-Extraktion fehlgeschlagen: %s", exc)
                return {**state, "attachment_error": "Das PDF konnte nicht gelesen werden."}
            # Extrahierter Text ist Fremdinhalt wie jede andere Nutzereingabe
            # -- dieselbe DLP-Prüfung, die main.py auf `message` bereits VOR
            # diesem Node durchführt (siehe landing_chat()-Docstring).
            dlp = SecurityLayer.check_and_redact(text)
            return {
                **state,
                "document_summary": dlp.redacted_text.strip()[:_MAX_DOCUMENT_TEXT_CHARS],
                "attachment_error": "",
            }

        if mime_type.startswith("image/"):
            try:
                response = self._llm.invoke([
                    cached_system_message(_SYSTEM_DOCUMENT_IMAGE),
                    HumanMessage(content=[
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime_type, "data": content_b64},
                        },
                    ]),
                ])
                description = response.content if isinstance(response.content, str) else str(response.content)
            except Exception as exc:
                logger.warning("document_node: Bildbeschreibung fehlgeschlagen: %s", exc)
                return {**state, "attachment_error": "Das Bild konnte nicht analysiert werden."}
            dlp = SecurityLayer.check_and_redact(description)
            return {**state, "document_summary": dlp.redacted_text.strip(), "attachment_error": ""}

        logger.info(
            "document_node: nicht unterstützter Dateityp",
            # "filename" ist ein reserviertes LogRecord-Attribut (der
            # Python-logging-Quelldatei-Name) -- ein extra-Key mit demselben
            # Namen wirft KeyError("Attempt to overwrite 'filename' ...")
            # beim Logging-Aufruf selbst, siehe Python-Doku zu Logger.info().
            extra={"mime_type": mime_type, "attachment_filename": filename},
        )
        return {**state, "attachment_error": f"Dateityp '{mime_type or 'unbekannt'}' wird aktuell nicht unterstützt."}

    # ── Node 3/4: appointment_node ───────────────────────────────────────────

    @resilient_node(fallback_builder=_inbound_chat_fallback)
    def appointment_node(self, state: InboundChatState) -> InboundChatState:
        """
        Entscheidet, ob dem Besucher JETZT aktiv ein Termin angeboten wird.

        "Gestión de la disponibilidad" heißt hier bewusst NICHT eine eigene
        Verfügbarkeitsabfrage gegen Google Calendar -- settings.demo_booking_url
        ist bereits eine selbstbedienende Google-Calendar-Terminseite (siehe
        core/config.py), die ihre eigene Verfügbarkeit verwaltet; eine
        zusätzliche Custom-Slot-Logik hier würde das nur duplizieren
        (dieselbe Architekturentscheidung wie der frühere Commit "feat:
        switch booking link to Google Calendar"). Dieser Node entscheidet
        nur, WANN der Link gezeigt wird -- identische Schwellenlogik wie
        vorher in finalize() (gleiche ICP-Schwelle wie der Outbound-Flow).
        """
        logger.info("Node: appointment_node", extra={"session": state["session_id"]})
        qualified = state["icp_score"] >= QUALIFICATION_THRESHOLD
        return {
            **state,
            "qualified": qualified,
            "booking_url": settings.demo_booking_url if qualified else None,
        }

    # ── Node 4/4: supervisor_node ────────────────────────────────────────────

    @resilient_node(fallback_builder=_inbound_chat_fallback)
    def supervisor_node(self, state: InboundChatState) -> InboundChatState:
        """
        Qualitätskontrolle + defensive JSON-Serialisierung, letzter Schritt
        vor der Antwort an main.py landing_chat(). Vereint drei vorher
        separate Verantwortlichkeiten:

        1. EU-AI-Act-Art.-50-Offenlegung deterministisch anhängen (identische
           Logik wie das frühere apply_disclosure()).
        2. Anhang-Zusammenfassung/-Fehler aus document_node deterministisch
           in die Antwort einweben (kein zweiter LLM-Call).
        3. final_result über _build_defensive_final_result() bauen statt
           eines einzigen dict-Literals, das bei einem unerwarteten Feldtyp
           komplett scheitern würde.

        Übernimmt außerdem Session-Persistenz und Lead-Capture/
        -Benachrichtigung (vorher Teil von finalize()) -- beides gehört
        inhaltlich hierher, weil es NACH der endgültigen reply_text-Fassung
        passieren muss.
        """
        logger.info("Node: supervisor_node", extra={"session": state["session_id"]})

        reply = state.get("reply_text") or ""
        if state.get("attachment_error"):
            reply = f"{reply}\n\n(Leider konnte ich deinen Anhang nicht verarbeiten: {state['attachment_error']})"
        elif state.get("document_summary"):
            reply = f"{reply}\n\n(Ich habe deine Datei erhalten und ausgewertet — das fließt in unser Gespräch ein.)"

        if state["is_first_turn"]:
            disclosure = AI_DISCLOSURE_DE.format(client_name=_CLIENT_NAME)
            if disclosure not in reply:
                reply = f"{reply}\n\n{disclosure}"

        history = state["history"] + [
            {"role": "user", "content": state["message"]},
            {"role": "assistant", "content": reply},
        ]
        history = history[-_MAX_INBOUND_HISTORY_TURNS:]

        _save_inbound_session(InboundChatSession(
            session_id=state["session_id"],
            history=history,
            icp_score=state["icp_score"],
            icp_rationale=state["icp_rationale"],
            company_name=state["company_name"],
            industry=state["industry"],
            pain_points=state["pain_points"],
            contact_name=state.get("contact_name", ""),
            turn_count=state["turn_count"] + 1,
            created_at=state["created_at"],
        ))

        # Lead-Capture (core/lead_capture.py): unabhängig von der ICP-
        # Qualifizierung unten -- ein Besucher kann Kontaktdaten nennen,
        # bevor genug über die Firma bekannt ist, um den ICP-Score zu heben.
        # Regex (E-Mail/Telefon, deterministisch) + bereits bekannte
        # visitor_info-Formulardaten + der LLM-extrahierte contact_name.
        # Der Anhang-Auszug fließt mit in die Lead-Mail ein (Anton sieht so
        # z. B. den Inhalt eines hochgeladenen Angebots direkt in der
        # Benachrichtigung), ohne die Erkennung selbst zu beeinflussen.
        contact_fields = lead_capture.extract_contact_fields(state["message"], state.get("visitor_info"))
        lead_message = state["message"]
        if state.get("document_summary"):
            lead_message = f"{lead_message}\n\n[Anhang-Auszug]\n{state['document_summary']}"
        new_lead = lead_capture.capture(
            source="landing_chat",
            session_id=state["session_id"],
            message=lead_message,
            name=state.get("contact_name") or contact_fields["name"],
            email=contact_fields["email"],
            phone=contact_fields["phone"],
            company=state["company_name"] or contact_fields["company"],
        )
        if new_lead is not None:
            # notify_lead_async() verschickt in einem Hintergrund-Thread und
            # kehrt sofort zurück (siehe tools/lead_notifier.py) --
            # supervisor_node läuft synchron innerhalb von main.py
            # landing_chat(), das dem Website-Besucher SOFORT antworten
            # muss. Ein SMTP-Ausfall (Netzwerk oder Credentials) darf diese
            # Antwort weder verzögern noch zu einem HTTP 400/500 führen.
            # Dieses try/except ist eine zusätzliche Absicherung on top von
            # notify_lead_async()s eigenem try/except (das selbst nie
            # wirft) -- schützt zusätzlich gegen einen Fehler beim
            # Thread-Start selbst (z. B. Ressourcenlimit).
            try:
                lead_notifier.notify_lead_async(new_lead)
            except Exception as exc:
                logger.warning("Lead-Benachrichtigung (landing_chat) fehlgeschlagen: %s", exc)

        qualified = bool(state.get("qualified", False))
        tier = "low"
        for label, threshold in _ICP_TIER_THRESHOLDS.items():
            if state["icp_score"] >= threshold:
                tier = label
                break

        # customer_state nur schreiben, wenn qualifiziert UND wenigstens ein
        # Identifier bekannt ist -- gleiches Muster wie write_to_crm() im
        # Outbound-Flow (kein CRM-/State-Eintrag für unqualifizierte Leads).
        # ANDERS als der Outbound-Flow: KEIN CRMIntegrationSDR.upsert_lead()
        # hier -- ein anonymer Chat-Besucher ist noch kein Lead-Datensatz,
        # nur eine Zeile im geteilten Kundenzustand.
        visitor_email = (state.get("visitor_info") or {}).get("email") or ""
        if qualified and (state["company_name"] or visitor_email):
            customer_state.update_stage(
                "sdr",
                {
                    "source": "landing_chat",
                    "icp_score": state["icp_score"],
                    "icp_tier": tier,
                    "industry": state["industry"],
                    "pain_points": state["pain_points"],
                },
                email=visitor_email or None,
                company_name=state["company_name"] or None,
                agent_session_id=state["session_id"],
            )

        final_result = _build_defensive_final_result(state, qualified, tier, reply)
        return {**state, "reply_text": reply, "final_result": final_result}

    # ── Routing ───────────────────────────────────────────────────────────────

    def _route_after_receptionist(self, state: InboundChatState) -> str:
        return "document_node" if state.get("attachment") else "appointment_node"

    # ── Graph Builder ─────────────────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(InboundChatState)
        graph.add_node("receptionist_node", self.receptionist_node)
        graph.add_node("document_node", self.document_node)
        graph.add_node("appointment_node", self.appointment_node)
        graph.add_node("supervisor_node", self.supervisor_node)

        graph.set_entry_point("receptionist_node")
        graph.add_conditional_edges(
            "receptionist_node",
            self._route_after_receptionist,
            {"document_node": "document_node", "appointment_node": "appointment_node"},
        )
        graph.add_edge("document_node", "appointment_node")
        graph.add_edge("appointment_node", "supervisor_node")
        graph.add_edge("supervisor_node", END)

        return graph.compile()

    def run(
        self,
        session_id: str,
        message: str,
        visitor_info: dict[str, Any],
        attachment: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        session = _get_inbound_session(session_id)
        initial: InboundChatState = {
            "session_id": session_id,
            "message": message,
            "visitor_info": visitor_info,
            "attachment": attachment,
            "history": session.history,
            "is_first_turn": len(session.history) == 0,
            "turn_count": session.turn_count,
            "created_at": session.created_at,
            "reply_text": "",
            "icp_score": session.icp_score,
            "icp_rationale": session.icp_rationale,
            "company_name": session.company_name,
            "industry": session.industry,
            "pain_points": session.pain_points,
            "contact_name": session.contact_name,
            "language": "de",
            "document_summary": "",
            "attachment_error": "",
            "qualified": False,
            "booking_url": None,
            "final_result": {},
        }
        final_state = self._graph.invoke(initial)
        return final_state["final_result"]


# ── SDRAgent ───────────────────────────────────────────────────────────────────

class SDRAgent(BaseAgent):
    """Öffentliche Agent-Klasse. Delegiert Logik an SDRGraph (LangGraph)."""

    agent_type = "sdr"

    def __init__(self) -> None:
        super().__init__()
        self._workflow = SDRGraph(
            llm=build_llm(max_tokens=1024),
            db=LeadDatabase(),
            # CRMIntegrationSDR ist standardmäßig ein In-Memory-Mock (Leads
            # gehen bei jedem Neustart verloren). Block C1 (10.09.2026) hat
            # optional die echte Kopplung an crm_handler.py (Repo
            # la-maquina-de-confianza) ergänzt — aktiv nur mit
            # SDR_CRM_LIVE_SHEET=true (core/config.py, Default: aus). NUR für
            # lokale Entwicklung geeignet: crm_handler.py nutzt einen an
            # DIESEN Mac gebundenen OAuth-Token, ein Railway-Deploy hat
            # keinen Zugriff darauf. Details: tools/crm_integration.py
            # (_lead_record_to_sheet_row, upsert_lead) und
            # tools/live_crm_bridge.py.
            crm=CRMIntegrationSDR(
                endpoint=settings.crm_endpoint,
                api_key=settings.crm_api_key.get_secret_value(),
            ),
        )
        # Eigener LLM-Client, kleineres max_tokens als der Outbound-Workflow
        # (1024) -- eine Chat-Antwort ist per Prompt-Vorgabe auf 2-5 Sätze
        # begrenzt, braucht also weniger Headroom als eine volle Outreach-
        # Nachricht. Getrennt vom Outbound-_llm, damit ein zukünftiges
        # Tuning des einen Workflows (z. B. Retries, anderes Modell) den
        # jeweils anderen nicht versehentlich mitbeeinflusst.
        self._inbound = InboundChatGraph(llm=build_llm(max_tokens=768))

    def _run(self, request: AgentRequest) -> dict[str, Any]:
        return self._workflow.run(
            input_text=request.text,
            session_id=request.session_id,
        )

    def process_inbound_chat(
        self,
        session_id: str,
        message: str,
        visitor_info: Optional[dict[str, Any]] = None,
        attachment: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Inbound-Modus für das Landing-Page-Chat-Widget (main.py:
        POST /api/v1/chat/landing). Umgeht bewusst BaseAgent.process()/
        AgentRequest: anders als die anderen 5 Agenten (ein zustandsloser
        Text-Request rein, ein Dict raus) ist das hier mehrstufiges Chat-
        State über mehrere Turns hinweg (session_id-basierter In-Memory-
        Verlauf, siehe InboundChatSession), und die Antwortform (reply,
        should_book_demo, booking_url) passt nicht ins generische
        AgentResponse.result-Schema. Die Input-DLP-Prüfung auf `message`
        läuft bereits VOR diesem Aufruf in main.py -- exakt dasselbe Muster
        wie beim strukturell ähnlichen /api/v1/webhooks/inbound-reply
        (dort ebenfalls kein AgentRequest, aus demselben Grund).

        `attachment` (main.py LandingAttachment.model_dump(), optional) wird
        unverändert an InboundChatGraph.run() durchgereicht -- document_node
        (Node 2/4) übernimmt Dekodierung/Größenprüfung/DLP auf den
        extrahierten Inhalt selbst.
        """
        return self._inbound.run(
            session_id=session_id, message=message, visitor_info=visitor_info or {}, attachment=attachment
        )
