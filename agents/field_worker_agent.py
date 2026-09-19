"""
Field Worker Agent – vollständig implementiert.

Kern des Baustellen-Voice-Assistant: nimmt eine (roh transkribierte oder
getippte) WhatsApp-Nachricht eines Technikers/Handwerkers auf einer
österreichischen Baustelle entgegen und extrahiert daraus strukturierte
Regiebericht-Daten (Techniker, Kunde, Stunden, Material, Tätigkeit). Der so
erzeugte final_result-Dict passt 1:1 auf die Eingabe-Signatur von
utils/pdf_generator.py generate_regiebericht() — main.py POST
/api/v1/webhook/whatsapp verbindet beide.

Workflow (LangGraph StateGraph):
  extract_entities  ← LLM: strukturierte Regiebericht-Felder aus Freitext
        ↓                   (österreichischer Dialekt/Fachjargon-tolerant)
  finalize
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from agents.base_agent import AgentRequest, BaseAgent
from core.llm import build_llm, cached_system_message

logger = logging.getLogger(__name__)


# ── JSON-Parsing ─────────────────────────────────────────────────────────────
# Identische mehrstufige Fallback-Logik wie agents/sdr_agent.py _parse_llm_json()
# (17.09.2026-Fix: "Expecting value: line 1 column 1", wenn das LLM in reinem
# Fließtext statt JSON antwortet) -- hier bewusst dupliziert statt importiert,
# damit field_worker_agent.py (wie voice_agent.py) unabhängig von sdr_agent.py
# bleibt und nicht versehentlich an dessen SDR-spezifische Logik gekoppelt wird.


def _extract_balanced_json_object(text: str) -> Optional[str]:
    """Findet die erste vollständige, klammer-balancierte '{...}'-Teilzeichenkette."""
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


def _parse_llm_json(text: str) -> dict:
    """Dreistufig: direktes json.loads, Codefence irgendwo im Text, balanciertes
    {...}-Objekt irgendwo im Text. Siehe agents/sdr_agent.py für den vollen
    Hintergrund/die Begründung dieser Reihenfolge."""
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

class FieldWorkerState(TypedDict):
    input_text: str
    session_id: str

    # set by extract_entities
    techniker: str
    kunde: str
    datum: str              # bewusst string, nicht date -- der Techniker nennt oft nur "heute"/"gestern" oder gar kein Datum
    stunden: Optional[float]
    material: list[str]
    arbeit: str
    language: str
    confidence_notes: str   # z. B. "Kundenname nicht eindeutig verstanden" -- für main.py, um im Zweifel nachzufragen

    final_result: dict[str, Any]


# ── LLM Prompt ─────────────────────────────────────────────────────────────────

_SYSTEM_EXTRACT = """\
Du bist ein Assistenzsystem für Handwerksbetriebe in Österreich (Wien-Raum) —
Elektriker, Installateure, Klimatechniker, Maler, Tischler und verwandte
Gewerke. Techniker schicken dir nach Feierabend oder direkt von der
Baustelle eine WhatsApp-Sprachnachricht oder Textnachricht, in der sie kurz
zusammenfassen, was sie heute gemacht haben — meist informell, oft in Wiener
Dialekt oder mit Fachjargon, manchmal unvollständig oder in Stichworten.

TYPISCHE MERKMALE, DIE DU KENNEN UND RICHTIG DEUTEN MUSST:
- Wiener/österreichischer Dialekt und Umgangssprache: "leiwand" (gut/erledigt),
  "eh" (ohnehin), "Oida" (Kumpel/Ausruf, ignorieren), "des" (das), "ma" (man/mir),
  "gmacht" (gemacht), "hob" (habe), "san" (sind), "a" (ein/auch)
- Österreichische Fachbegriffe: "Sicherungskasten"/"FI-Schalter"/"Verteiler"
  (Elektrik), "Therme"/"Heizkörper"/"Ventil" (Installateur/Klimatechnik),
  "Spachtel"/"Grundierung" (Maler), "Zarge"/"Blende" (Tischler)
- Gesprochene Zahlen/Zeitangaben: "dreieinhalb Stunden" = 3.5, "a halbe Stunde"
  = 0.5, "an ganzen Vormittag" = ca. 4 Stunden (nur schätzen, wenn keine
  genauere Angabe möglich ist — dann in confidence_notes vermerken)
- Materialangaben oft beiläufig erwähnt ("hab no a Kabel und a Sicherung
  mitgenommen") statt als saubere Liste

DEINE AUFGABE: Extrahiere aus der Nachricht strukturierte Daten für einen
Regiebericht (das Standard-Abrechnungsdokument im DACH-Bauhandwerk). Formuliere
das Feld "arbeit" in klarem, professionellem Hochdeutsch um — der Regiebericht
geht an den Kunden, Dialekt/Umgangssprache gehören NICHT ins fertige Dokument,
auch wenn die Nachricht selbst so klingt. Erfinde NIEMALS Details (Kundenname,
Material, Stunden), die nicht in der Nachricht vorkommen oder sich daraus
eindeutig erschließen lassen — fehlende Angaben bleiben leer/null, nicht geraten.

Gib AUSSCHLIESSLICH valides JSON zurück (kein Text davor/danach):
{
  "techniker": string (Name des Technikers, falls in der Nachricht genannt, sonst ""),
  "kunde": string (Kunde, Firma oder Baustellen-/Projektbezeichnung, sonst ""),
  "datum": string (Datum im Format TT.MM.JJJJ, falls explizit genannt oder eindeutig
                    aus Kontext wie "heute"/"gestern" ableitbar, sonst ""),
  "stunden": number oder null (geleistete Arbeitsstunden als Dezimalzahl,
                    z.B. 3.5 für "dreieinhalb Stunden"),
  "material": [Liste von Strings, je ein Material-/Teilenamen, sonst leere Liste],
  "arbeit": string (2-5 Sätze, professionelles Hochdeutsch, sachliche Beschreibung
                    der durchgeführten Tätigkeit — Basis für den Regiebericht),
  "language": "de" | "en",
  "confidence_notes": string (kurzer Hinweis auf unsichere/geschätzte/fehlende
                    Angaben, z.B. "Stundenzahl geschätzt, nicht explizit genannt"
                    oder "Kundenname nicht erwähnt" — leerer String, wenn alles
                    eindeutig war)
}
"""


# ── Graph ──────────────────────────────────────────────────────────────────────

class FieldWorkerGraph:
    """LangGraph-Workflow für den Baustellen-Voice-Assistant."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm
        self._graph = self._build_graph()

    # ── Node: extract_entities ───────────────────────────────────────────────

    def extract_entities(self, state: FieldWorkerState) -> FieldWorkerState:
        logger.info("Node: extract_entities", extra={"session": state["session_id"]})

        defaults: dict[str, Any] = {
            "techniker": "",
            "kunde": "",
            "datum": "",
            "stunden": None,
            "material": [],
            "arbeit": state["input_text"][:500],
            "language": "de",
            "confidence_notes": "LLM-Extraktion fehlgeschlagen — Rohtext als Arbeitsbeschreibung übernommen.",
        }

        try:
            response = self._llm.invoke([
                cached_system_message(_SYSTEM_EXTRACT),
                HumanMessage(content=state["input_text"]),
            ])
        except Exception as exc:
            # LLM-Aufruf selbst fehlgeschlagen (Netzwerk, Rate-Limit, ...) --
            # kein Antworttext zu retten, siehe agents/sdr_agent.py
            # receptionist_node() für dieselbe zweistufige Philosophie.
            logger.warning("extract_entities: LLM-Aufruf fehlgeschlagen: %s", exc)
            return {**state, **defaults}

        try:
            data = _parse_llm_json(response.content)
        except Exception as exc:
            # LLM hat geantwortet, aber nicht im geforderten JSON-Format --
            # Rohtext als Arbeitsbeschreibung übernehmen statt die gesamte
            # Nachricht zu verwerfen (der Techniker hat trotzdem etwas
            # geschickt, das dokumentiert werden soll).
            logger.warning(
                "extract_entities: LLM-Antwort war kein valides JSON, nutze Rohtext: %s", exc
            )
            return {**state, **defaults}

        material = data.get("material")
        if not isinstance(material, list):
            material = [str(material)] if material else []

        stunden_raw = data.get("stunden")
        try:
            stunden = float(stunden_raw) if stunden_raw is not None else None
        except (TypeError, ValueError):
            stunden = None

        return {
            **state,
            "techniker": data.get("techniker") or defaults["techniker"],
            "kunde": data.get("kunde") or defaults["kunde"],
            "datum": data.get("datum") or defaults["datum"],
            "stunden": stunden,
            "material": [str(m) for m in material if str(m).strip()],
            "arbeit": data.get("arbeit") or defaults["arbeit"],
            "language": data.get("language") or defaults["language"],
            "confidence_notes": data.get("confidence_notes", ""),
        }

    # ── Node: finalize ───────────────────────────────────────────────────────

    def finalize(self, state: FieldWorkerState) -> FieldWorkerState:
        logger.info("Node: finalize", extra={"session": state["session_id"]})

        # Struktur passt bewusst 1:1 auf utils/pdf_generator.py
        # generate_regiebericht()s erwartetes data-dict -- main.py reicht
        # final_result direkt (ggf. mit visitor-ergänzten Feldern) durch.
        final: dict[str, Any] = {
            "techniker": state["techniker"],
            "kunde": state["kunde"],
            "datum": state["datum"],
            "stunden": state["stunden"],
            "material": state["material"],
            "arbeit": state["arbeit"],
            "language": state["language"],
            "confidence_notes": state["confidence_notes"],
            "vollstaendig": bool(state["techniker"] and state["kunde"] and state["arbeit"]),
        }
        return {**state, "final_result": final}

    # ── Graph Builder ─────────────────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(FieldWorkerState)
        graph.add_node("extract_entities", self.extract_entities)
        graph.add_node("finalize", self.finalize)

        graph.set_entry_point("extract_entities")
        graph.add_edge("extract_entities", "finalize")
        graph.add_edge("finalize", END)

        return graph.compile()

    def run(self, input_text: str, session_id: str) -> dict[str, Any]:
        initial: FieldWorkerState = {
            "input_text": input_text,
            "session_id": session_id,
            "techniker": "",
            "kunde": "",
            "datum": "",
            "stunden": None,
            "material": [],
            "arbeit": "",
            "language": "de",
            "confidence_notes": "",
            "final_result": {},
        }
        return self._graph.invoke(initial)["final_result"]


# ── FieldWorkerAgent ─────────────────────────────────────────────────────────

class FieldWorkerAgent(BaseAgent):
    """
    Extrahiert strukturierte Regiebericht-Daten aus einer Techniker-Nachricht
    (WhatsApp-Text oder -Transkript). Öffentliche Agent-Klasse, delegiert an
    FieldWorkerGraph (LangGraph) -- Standard-BaseAgent-Interface, KEIN
    Sonderfall wie InboundChatGraph/VoiceAgent: ein zustandsloser Text-Request
    rein, ein strukturiertes Dict raus, passt 1:1 in AgentRequest/AgentResponse.
    Läuft daher automatisch durch BaseAgent.process()s Input-/Output-DLP.
    """

    agent_type = "field-worker"

    def __init__(self) -> None:
        super().__init__()
        # max_tokens=768: eine Regiebericht-Extraktion ist deutlich kürzer als
        # ein voller Outreach-Text (agents/sdr_agent.py build_llm(max_tokens=1024)),
        # aber die "arbeit"-Beschreibung (2-5 Sätze) + Material-Liste braucht
        # mehr Headroom als InboundChatGraphs 768 für eine reine Chat-Antwort
        # — gleicher Wert, aber aus unabhängiger Abwägung für diesen Use-Case.
        self._workflow = FieldWorkerGraph(llm=build_llm(max_tokens=768))

    def _run(self, request: AgentRequest) -> dict[str, Any]:
        return self._workflow.run(
            input_text=request.text,
            session_id=request.session_id,
        )
