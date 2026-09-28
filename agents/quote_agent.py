"""
Quote Agent -- Angebots-Generator für Pilotkunden (Pain 2: "Angebote abends
manuell tippen", siehe novara_wissen.txt TOP-3-SCHMERZEN).

Workflow (LangGraph StateGraph):
  extract_request   ← LLM: strukturiert NUR Mengen/Stunden/Beschreibungen aus
        ↓              dem Anfragetext (WhatsApp-Text/Sprachnachricht-
        ↓              Transkript) -- extrahiert NIEMALS einen Preis, siehe
        ↓              _SYSTEM_EXTRACT weiter unten.
  price_positions   ← DETERMINISTISCH, KEIN LLM: rechnet Stunden x
        ↓              client_profile.stundensatz_eur. Keine Stunden-Schätzung
        ↓              oder kein hinterlegter Stundensatz -> Position wird als
        ↓              "nach Aufwand" markiert (kein erfundener Preis, exakt
        ↓              wie vom Kunden gefordert -- gleiche Philosophie wie
        ↓              core/outbound_guard.py: nur Zahlen aus echten Daten).
        ↓              Zusätzlich, falls hinterlegt: client_profile.
        ↓              materialaufschlag_pct als Pauschal-Posten auf die
        ↓              bepreiste Lohnsumme (kein Materialpreis-Katalog, kein
        ↓              vom LLM geschätzter Materialwert -- siehe Kommentar
        ↓              direkt im Code).
  generate_pdf      ← utils/pdf_generator.py::generate_angebot()
        ↓
  persist           ← tools/quote_store.create_quote() (Status
                       "pending_approval", Freigabe-Token)

WICHTIG: Dieser Agent sendet NIE direkt an den Endkunden. Das PDF geht immer
zuerst als Entwurf mit Freigabe-Link an den Elektriker selbst (main.py) --
main.py POST /api/v1/quotes/{id}/approve schaltet erst danach den Versand
an den Kunden frei.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from agents.base_agent import AgentRequest, BaseAgent
from core.client_profiles import ClientProfile, ClientProfileNotFoundError, load_client_profile
from core.llm import build_llm, cached_system_message
from tools import quote_store
from utils.pdf_generator import generate_angebot

logger = logging.getLogger(__name__)

_QUOTES_DIR = Path("quotes_tmp")  # Railway: flüchtig, PDF wird direkt nach WhatsApp-Versand gelöscht (main.py)


def _strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped


def _parse_positions_json(text: str) -> dict:
    """Best-Effort-JSON-Parse für die extract_request()-Antwort. Kein
    Salvage-Mechanismus wie bei sdr_agent.py nötig -- diese Antwort enthält
    keinen frei formulierten "reply"-Fließtext (der bei sdr_agent.py an
    kaputten Anführungszeichen scheitert), sondern nur kurze, strukturierte
    Felder. Bei Parse-Fehler: leere Positionsliste statt Absturz -- main.py
    zeigt dem Elektriker dann "konnte nichts extrahieren" statt eines Fehlers."""
    body = _strip_markdown_fence(text)
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        logger.warning("quote_agent: LLM-Antwort war kein valides JSON: %r", text[:200])
        return {}


_SYSTEM_EXTRACT = """\
Du extrahierst aus einer Kundenanfrage (WhatsApp-Text oder Sprachnachrichten-
Transkript) an einen Elektrikerbetrieb strukturierte Angebots-Positionen.

Der Betrieb bietet folgende Leistungen an: {leistungen}

REGELN (STRIKT):
1. Erfinde NIEMALS einen Preis oder eine Kostenschätzung -- das ist NICHT
   deine Aufgabe. Extrahiere ausschließlich Mengen, Stunden und
   Beschreibungen aus dem Text.
2. Schätze "geschaetzte_stunden" NUR, wenn der Aufwand aus dem Text
   plausibel ableitbar ist (z. B. "3 Steckdosen setzen" -> grobe
   Handwerker-Erfahrungswerte). Ist der Aufwand unklar oder stark variabel
   (z. B. "Komplettsanierung", "Fehlersuche"), setze "geschaetzte_stunden"
   auf null -- dann wird die Position im nächsten Schritt korrekt als
   "nach Aufwand" markiert, statt eine falsche Zahl zu raten.
3. Kundenname/Kontakt nur übernehmen, wenn im Text wirklich genannt --
   nichts erfinden.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt, keine Erklärung davor/danach:
{{
  "kunde_name": "" ,
  "kunde_kontakt": "",
  "dringlichkeit": "normal" ,
  "positionen": [
    {{"beschreibung": "...", "menge": 1, "einheit": "Stk.", "geschaetzte_stunden": 0.5}}
  ]
}}
"""


class QuoteState(TypedDict):
    input_text: str
    session_id: str
    client_id: str

    kunde_name: str
    kunde_kontakt: str
    dringlichkeit: str
    positionen: list[dict]

    priced_positionen: list[dict]
    gesamtsumme_eur: Optional[float]
    hat_offene_positionen: bool

    pdf_path: str
    quote_id: str
    approval_token: str

    final_result: dict
    error: Optional[str]


class _QuoteGraph:
    def __init__(self) -> None:
        self._llm = build_llm(max_tokens=1024)
        self._graph = self._build_graph()

    def extract_request(self, state: QuoteState) -> QuoteState:
        try:
            profile = load_client_profile(state["client_id"])
        except ClientProfileNotFoundError as exc:
            return {**state, "error": str(exc)}

        system = _SYSTEM_EXTRACT.format(leistungen=", ".join(profile.leistungen) or "allgemeine Elektrikerarbeiten")
        response = self._llm.invoke([cached_system_message(system), HumanMessage(content=state["input_text"])])
        data = _parse_positions_json(response.content if hasattr(response, "content") else str(response))

        return {
            **state,
            "kunde_name": str(data.get("kunde_name") or ""),
            "kunde_kontakt": str(data.get("kunde_kontakt") or ""),
            "dringlichkeit": str(data.get("dringlichkeit") or "normal"),
            "positionen": data.get("positionen") or [],
        }

    def price_positions(self, state: QuoteState) -> QuoteState:
        if state.get("error"):
            return state
        try:
            profile: ClientProfile = load_client_profile(state["client_id"])
        except ClientProfileNotFoundError as exc:
            return {**state, "error": str(exc)}

        priced: list[dict] = []
        summe = 0.0
        hat_offene = False

        for pos in state["positionen"]:
            stunden = pos.get("geschaetzte_stunden")
            menge = pos.get("menge") or 1

            # Kein Stundensatz hinterlegt ODER kein Stundenschätzwert vom LLM
            # -> Position bleibt UNBEPREIST ("nach Aufwand"). Kein Fallback-
            # Schätzwert hier, exakt wie vom Kunden gefordert: das LLM darf
            # keine Preise erfinden, und dieser Code auch nicht.
            if isinstance(stunden, (int, float)) and profile.stundensatz_eur:
                einzelpreis = round(stunden * profile.stundensatz_eur, 2)
                gesamtpreis = round(einzelpreis * float(menge), 2)
                summe += gesamtpreis
            else:
                einzelpreis = None
                gesamtpreis = None
                hat_offene = True

            priced.append({
                "beschreibung": pos.get("beschreibung") or "-",
                "menge": menge,
                "einheit": pos.get("einheit") or "Stk.",
                "einzelpreis_eur": einzelpreis,
                "gesamtpreis_eur": gesamtpreis,
            })

        # Materialaufschlag (BUG-FIX: profile.materialaufschlag_pct existierte
        # bereits im Schema (core/client_profiles.py), wurde hier aber nie
        # gelesen -- jedes Angebot bestand nur aus Lohnkosten +
        # Anfahrtspauschale, ohne jeden Materialanteil. Für einen
        # Elektrikerbetrieb ist das unrealistisch: praktisch jede Position
        # (Steckdose setzen, Kabel verlegen, ...) verbraucht Material, das der
        # Betrieb selbst eingekauft und mit einem Aufschlag weiterverrechnet.
        #
        # Kein per-Position-Materialpreis-Katalog existiert (und das LLM darf
        # laut _SYSTEM_EXTRACT keine Materialkosten erfinden) -- deterministisch
        # wird daher derselbe Aufschlag, den der Betrieb selbst im Profil
        # hinterlegt hat, als EIN Pauschal-Posten auf die bereits bepreiste
        # Lohnsumme draufgerechnet (gleiche Philosophie wie
        # anfahrtspauschale_eur: nur echte, vom Betrieb gepflegte Zahlen,
        # nie ein Schätzwert). Kein Lohnanteil bekannt (summe == 0, z. B. weil
        # jede Position "nach Aufwand" ist) -> kein Materialposten, es gäbe
        # sonst nichts, worauf sich der Aufschlag bezöge.
        if profile.materialaufschlag_pct and summe > 0:
            material_pct = float(profile.materialaufschlag_pct)
            materialkosten = round(summe * material_pct / 100, 2)
            if materialkosten > 0:
                priced.append({
                    "beschreibung": f"Material (pauschal, {material_pct:g}% vom Arbeitsaufwand)",
                    "menge": 1,
                    "einheit": "Pauschal",
                    "einzelpreis_eur": materialkosten,
                    "gesamtpreis_eur": materialkosten,
                })
                summe += materialkosten

        if profile.anfahrtspauschale_eur:
            priced.append({
                "beschreibung": "Anfahrtspauschale",
                "menge": 1,
                "einheit": "Pauschal",
                "einzelpreis_eur": profile.anfahrtspauschale_eur,
                "gesamtpreis_eur": profile.anfahrtspauschale_eur,
            })
            summe += profile.anfahrtspauschale_eur

        return {
            **state,
            "priced_positionen": priced,
            "gesamtsumme_eur": round(summe, 2) if priced else None,
            "hat_offene_positionen": hat_offene,
        }

    def generate_pdf(self, state: QuoteState) -> QuoteState:
        if state.get("error"):
            return state
        try:
            profile = load_client_profile(state["client_id"])
        except ClientProfileNotFoundError as exc:
            return {**state, "error": str(exc)}

        if not state["priced_positionen"]:
            return {**state, "error": "Keine Positionen aus der Anfrage extrahiert -- kein Angebot erstellt."}

        _QUOTES_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", state["client_id"])[:40]
        out_path = _QUOTES_DIR / f"Angebot_{safe_name}_{state['session_id'][:8]}.pdf"

        generate_angebot(
            firmenname=profile.firmenname,
            kunde_name=state["kunde_name"] or "Kunde",
            positionen=state["priced_positionen"],
            gesamtsumme=state["gesamtsumme_eur"],
            hat_offene_positionen=state["hat_offene_positionen"],
            output_path=out_path,
        )
        return {**state, "pdf_path": str(out_path)}

    def persist(self, state: QuoteState) -> QuoteState:
        if state.get("error"):
            return {**state, "final_result": {"success": False, "error": state["error"]}}

        created = quote_store.create_quote(
            client_id=state["client_id"],
            kunde_name=state["kunde_name"],
            kunde_kontakt=state["kunde_kontakt"],
            positionen=state["priced_positionen"],
            gesamtsumme_eur=state["gesamtsumme_eur"],
            hat_offene_positionen=state["hat_offene_positionen"],
            pdf_path=state["pdf_path"],
        )
        return {
            **state,
            "quote_id": created["id"],
            "approval_token": created["approval_token"],
            "final_result": {
                "success": True,
                "quote_id": created["id"],
                "approval_token": created["approval_token"],
                "pdf_path": state["pdf_path"],
                "kunde_name": state["kunde_name"],
                "gesamtsumme_eur": state["gesamtsumme_eur"],
                "hat_offene_positionen": state["hat_offene_positionen"],
                "dringlichkeit": state["dringlichkeit"],
            },
        }

    def _build_graph(self):
        graph = StateGraph(QuoteState)
        graph.add_node("extract_request", self.extract_request)
        graph.add_node("price_positions", self.price_positions)
        graph.add_node("generate_pdf", self.generate_pdf)
        graph.add_node("persist", self.persist)

        graph.set_entry_point("extract_request")
        graph.add_edge("extract_request", "price_positions")
        graph.add_edge("price_positions", "generate_pdf")
        graph.add_edge("generate_pdf", "persist")
        graph.add_edge("persist", END)

        return graph.compile()

    def run(self, input_text: str, session_id: str, client_id: str) -> dict[str, Any]:
        initial: QuoteState = {
            "input_text": input_text,
            "session_id": session_id,
            "client_id": client_id,
            "kunde_name": "",
            "kunde_kontakt": "",
            "dringlichkeit": "normal",
            "positionen": [],
            "priced_positionen": [],
            "gesamtsumme_eur": None,
            "hat_offene_positionen": False,
            "pdf_path": "",
            "quote_id": "",
            "approval_token": "",
            "final_result": {},
            "error": None,
        }
        return self._graph.invoke(initial)["final_result"]


class QuoteAgent(BaseAgent):
    """BaseAgent-Wrapper (DLP/Logging automatisch, siehe agents/base_agent.py).
    `client_id` kommt über request.metadata, nicht über request.text --
    QuoteState.client_id bestimmt, welches Betriebsprofil (Preise/Leistungen)
    verwendet wird."""

    agent_type = "quote"

    def __init__(self) -> None:
        super().__init__()
        self._graph = _QuoteGraph()

    def _run(self, request: AgentRequest) -> dict[str, Any]:
        client_id = str(request.metadata.get("client_id") or "").strip()
        if not client_id:
            raise ValueError("QuoteAgent benötigt request.metadata['client_id']")
        return self._graph.run(request.text, request.session_id, client_id)
