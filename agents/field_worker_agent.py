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
        ↓                   (österreichischer Dialekt/Fachjargon-tolerant, jede Sprache)
  evaluate          ← Code: echte Arbeitsinformation + Pflichtangaben (Tätigkeit,
        ↓               Kunde, Stunden)? Nur dann ready_for_pdf=True
  compose_guidance  ← nur wenn KEIN PDF: freundlicher Text in der Sprache des Technikers
        ↓
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
    is_work_report: Optional[bool]   # LLM-Einstufung: beschreibt die Nachricht echte Arbeit? None = unbekannt
    extraction_failed: bool          # LLM-Aufruf/-Antwort unbrauchbar -> KEIN PDF, Bitte um erneutes Senden
    missing_fields: list[str]        # fehlende Pflichtangaben (arbeit/kunde/stunden), im Code bestimmt
    ready_for_pdf: bool              # NUR True, wenn echte Arbeitsinformation mit allen Pflichtangaben vorliegt
    guidance_type: str               # "" | "greeting" | "missing" | "retry"
    reply: str                       # Antworttext an den Techniker, wenn KEIN PDF erzeugt wird (in seiner Sprache)

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
Die Nachricht kann in JEDER Sprache kommen (Deutsch, Spanisch, Englisch,
Türkisch, Serbisch, Rumänisch, Polnisch, ...) -- erkenne die Sprache und extrahiere
trotzdem alle Felder; eine reine Begrüßung ist KEIN Arbeitsbericht.

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
  "language": string (ISO-639-1-Code der Sprache, in der der Techniker geschrieben bzw.
                    gesprochen hat, z. B. "de", "es", "en", "tr", "sr", "ro", "pl" --
                    "arbeit" bleibt trotzdem IMMER Hochdeutsch),
  "is_work_report": true oder false (true NUR, wenn die Nachricht tatsächlich
                    durchgeführte Arbeit beschreibt. Begrüßungen ("Hallo", "Servus", "hola"),
                    Danke, Smalltalk, Fragen, Testnachrichten und alles ohne konkrete
                    Arbeitsinformation sind false -- dann "arbeit" leer lassen),
  "confidence_notes": string (kurzer Hinweis auf unsichere/geschätzte/fehlende
                    Angaben, z.B. "Stundenzahl geschätzt, nicht explizit genannt"
                    oder "Kundenname nicht erwähnt" — leerer String, wenn alles
                    eindeutig war)
}
"""


_SYSTEM_GUIDANCE = """\
Du bist der freundliche WhatsApp-Assistent von Novara Automation für Handwerker
und Techniker auf Baustellen in Österreich. Aus einer Text- oder Sprachnachricht
erstellst du automatisch einen Regiebericht (Arbeitsnachweis) als PDF.

Schreibe jetzt eine KURZE, freundliche Antwort (höchstens 4 Sätze) an den
Techniker -- in DERSELBEN Sprache, in der er geschrieben bzw. gesprochen hat
(Sprachcode steht in der Anfrage). Nur der Antworttext als Freitext, KEIN JSON, kein
Markdown, keine Überschriften. Ein bis zwei passende Emojis sind erlaubt.

Die Anfrage nennt die Situation:
- GREETING: Begrüßung, Danke, Smalltalk oder eine Frage ohne Arbeitsinformation.
  Erwidere die Begrüßung kurz, erkläre in einem Satz, dass du aus einer Text- oder
  Sprachnachricht einen Regiebericht erstellst, und gib EIN kurzes Beispiel in
  seiner Sprache, was er schicken soll (Kunde/Baustelle, Arbeitsstunden,
  durchgeführte Arbeit, ggf. Material).
- MISSING: Es wurde Arbeit beschrieben, aber Pflichtangaben fehlen. Bedanke dich
  kurz, sage, dass der Bericht noch NICHT erstellt wurde, und bitte NUR um die
  genannten fehlenden Angaben.

Regeln: erfinde nichts, behaupte nie, ein PDF sei erstellt worden, keine Preise,
keine Versprechen über Fristen.
"""

_FIELD_LABELS: dict[str, dict[str, str]] = {
    "de": {"arbeit": "die durchgeführte Arbeit", "kunde": "den Kunden bzw. die Baustelle", "stunden": "die Arbeitsstunden"},
    "es": {"arbeit": "el trabajo realizado", "kunde": "el cliente o la obra", "stunden": "las horas trabajadas"},
    "en": {"arbeit": "the work performed", "kunde": "the customer or site", "stunden": "the hours worked"},
}

_TEMPLATES: dict[str, dict[str, str]] = {
    "de": {
        "greeting": (
            "Hallo! \U0001f44b Ich bin der Novara-Assistent für Regieberichte. Schick mir einfach kurz, was du "
            "heute gemacht hast -- als Text oder Sprachnachricht. Zum Beispiel: \"Heute 2 Stunden bei Familie "
            "Berger, Verteilerkasten getauscht, 1 FI-Schalter.\" Ich erstelle daraus dein PDF."
        ),
        "missing": (
            "Danke! Damit ich deinen Regiebericht erstellen kann, fehlt mir noch: {missing}. "
            "Schick mir das bitte kurz als Text oder Sprachnachricht."
        ),
        "retry": (
            "Entschuldigung, ich konnte deine Nachricht gerade nicht auswerten. "
            "Bitte schick sie in ein paar Minuten noch einmal."
        ),
    },
    "es": {
        "greeting": (
            "¡Hola! \U0001f44b Soy el asistente de Novara para partes de trabajo. Cuéntame brevemente qué has hecho "
            "hoy, por texto o nota de voz. Por ejemplo: \"Hoy 2 horas en casa de la familia Berger, cambié el "
            "cuadro eléctrico, 1 diferencial.\" Con eso te preparo el PDF."
        ),
        "missing": (
            "¡Gracias! Para poder crear tu parte de trabajo me falta: {missing}. "
            "Envíamelo por favor, por texto o nota de voz."
        ),
        "retry": (
            "Lo siento, no he podido procesar tu mensaje ahora mismo. "
            "Por favor, vuelve a enviarlo en unos minutos."
        ),
    },
    "en": {
        "greeting": (
            "Hello! \U0001f44b I'm Novara's assistant for work reports. Just tell me briefly what you did today, "
            "as text or a voice message. For example: \"Today 2 hours at the Berger house, replaced the "
            "distribution board, 1 RCD.\" I'll turn it into your PDF."
        ),
        "missing": (
            "Thanks! To create your work report I still need: {missing}. "
            "Please send it as text or a voice message."
        ),
        "retry": (
            "Sorry, I couldn't process your message just now. "
            "Please send it again in a few minutes."
        ),
    },
}

_STOPWORDS: dict[str, set[str]] = {
    "es": {"hola", "que", "qué", "el", "la", "los", "las", "un", "una", "hoy", "para", "por", "con", "de", "del",
           "gracias", "buenas", "buenos", "días", "dias", "tardes", "horas", "trabajo", "cliente", "estoy", "he"},
    "en": {"hello", "hi", "hey", "the", "and", "today", "for", "with", "hours", "work", "thanks", "thank", "you",
           "customer", "did", "have", "was", "at"},
    "de": {"hallo", "servus", "griaß", "moin", "der", "die", "das", "und", "heute", "für", "mit", "stunden", "arbeit",
           "danke", "kunde", "hab", "habe", "bei", "ich", "ist", "nicht"},
}


def _guess_language(text: str) -> str:
    """Grobe, rein lokale Spracherkennung (de/es/en) für die Fallback-Vorlagen,
    wenn das LLM keine brauchbare Sprache geliefert hat. Standard: Deutsch."""
    words = re.findall(r"[a-zA-ZäöüÄÖÜßáéíóúñÁÉÍÓÚÑ]+", (text or "").lower())
    scores = {lang: sum(1 for w in words if w in vocab) for lang, vocab in _STOPWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "de"


def _normalize_language(code: Any, text: str) -> str:
    """ISO-639-1-Kleinbuchstaben-Code aus der LLM-Angabe, sonst lokale Schätzung."""
    if isinstance(code, str):
        c = code.strip().lower()[:2]
        if len(c) == 2 and c.isalpha():
            return c
    return _guess_language(text)


def _template_reply(kind: str, language: str, missing: list[str]) -> str:
    lang = language if language in _TEMPLATES else "de"
    labels = _FIELD_LABELS[lang]
    missing_text = ", ".join(labels[m] for m in missing if m in labels)
    return _TEMPLATES[lang][kind].format(missing=missing_text)


_KEY_FIELDS = ("arbeit", "kunde", "stunden")


def _missing_key_fields(state: "FieldWorkerState") -> list[str]:
    """Pflichtangaben für einen Regiebericht: Tätigkeit, Kunde/Baustelle, Stunden."""
    missing: list[str] = []
    if not (state.get("arbeit") or "").strip():
        missing.append("arbeit")
    if not (state.get("kunde") or "").strip():
        missing.append("kunde")
    hours = state.get("stunden")
    if hours is None or hours <= 0:
        missing.append("stunden")
    return missing


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
            "is_work_report": None,
            "extraction_failed": True,
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
            return {**state, **defaults, "language": _guess_language(state["input_text"])}

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
            return {**state, **defaults, "language": _guess_language(state["input_text"])}

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
            # KEIN Rohtext-Fallback bei erfolgreicher Extraktion: ein leeres "arbeit"
            # heißt "keine Arbeitsinformation" (z. B. ein Gruß) und darf nicht mit der
            # Originalnachricht ("hola") aufgefüllt werden, sonst gälte sie als Arbeit.
            "arbeit": str(data.get("arbeit") or "").strip(),
            "language": _normalize_language(data.get("language"), state["input_text"]),
            "confidence_notes": data.get("confidence_notes", ""),
            "is_work_report": data.get("is_work_report") if isinstance(data.get("is_work_report"), bool) else None,
            "extraction_failed": False,
        }

    # ── Node: evaluate ───────────────────────────────────────────────────────
    # Deterministische Entscheidung IM CODE (nicht im LLM): ein PDF entsteht nur
    # bei echter Arbeitsinformation MIT allen Pflichtangaben. Alles andere
    # (Gruß, Smalltalk, unvollständige Angaben, Extraktionsfehler) bekommt
    # ausschließlich einen freundlichen Text in der Sprache des Technikers.

    def evaluate(self, state: FieldWorkerState) -> FieldWorkerState:
        if state["extraction_failed"]:
            return {**state, "ready_for_pdf": False, "missing_fields": [], "guidance_type": "retry"}
        if state["is_work_report"] is False:
            return {**state, "ready_for_pdf": False, "missing_fields": [], "guidance_type": "greeting"}
        missing = _missing_key_fields(state)
        if missing:
            # Nichts Verwertbares an Arbeit genannt (z. B. LLM ohne Einstufung, aber alles leer)
            # ist inhaltlich ein Gruß, keine "unvollständige Meldung".
            kind = "greeting" if set(missing) == set(_KEY_FIELDS) else "missing"
            return {**state, "ready_for_pdf": False, "missing_fields": missing if kind == "missing" else [], "guidance_type": kind}
        return {**state, "ready_for_pdf": True, "missing_fields": [], "guidance_type": ""}

    @staticmethod
    def _route_after_evaluate(state: FieldWorkerState) -> str:
        return "compose_guidance" if state["guidance_type"] in ("greeting", "missing") else "finalize"

    # ── Node: compose_guidance ───────────────────────────────────────────────

    def compose_guidance(self, state: FieldWorkerState) -> FieldWorkerState:
        logger.info("Node: compose_guidance", extra={"session": state["session_id"], "type": state["guidance_type"]})
        kind = state["guidance_type"]
        lang = state["language"]
        situation = "GREETING" if kind == "greeting" else "MISSING"
        request = f"Sprachcode: {lang}\nSituation: {situation}\n"
        if kind == "missing":
            labels = _FIELD_LABELS["en"]
            request += "Fehlende Angaben: " + ", ".join(labels[m] for m in state["missing_fields"]) + "\n"
        request += f"Nachricht des Technikers: {state['input_text'][:500]}"

        reply = ""
        try:
            response = self._llm.invoke([cached_system_message(_SYSTEM_GUIDANCE), HumanMessage(content=request)])
            reply = str(response.content or "").strip()[:700]
        except Exception as exc:
            logger.warning("compose_guidance: LLM-Aufruf fehlgeschlagen, nutze Vorlage: %s", exc)
        if not reply:
            reply = _template_reply(kind, lang, state["missing_fields"])
        return {**state, "reply": reply}

    # ── Node: finalize ───────────────────────────────────────────────────────

    def finalize(self, state: FieldWorkerState) -> FieldWorkerState:
        logger.info("Node: finalize", extra={"session": state["session_id"]})

        reply = state["reply"]
        if not state["ready_for_pdf"] and not reply:
            reply = _template_reply(state["guidance_type"] or "retry", state["language"], state["missing_fields"])

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
            "is_work_report": state["is_work_report"],
            "ready_for_pdf": state["ready_for_pdf"],
            "missing_fields": state["missing_fields"],
            "guidance_type": state["guidance_type"],
            "reply": reply,
        }
        return {**state, "final_result": final}

    # ── Graph Builder ─────────────────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(FieldWorkerState)
        graph.add_node("extract_entities", self.extract_entities)
        graph.add_node("evaluate", self.evaluate)
        graph.add_node("compose_guidance", self.compose_guidance)
        graph.add_node("finalize", self.finalize)

        graph.set_entry_point("extract_entities")
        graph.add_edge("extract_entities", "evaluate")
        graph.add_conditional_edges(
            "evaluate", self._route_after_evaluate,
            {"compose_guidance": "compose_guidance", "finalize": "finalize"},
        )
        graph.add_edge("compose_guidance", "finalize")
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
            "is_work_report": None,
            "extraction_failed": False,
            "missing_fields": [],
            "ready_for_pdf": False,
            "guidance_type": "",
            "reply": "",
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
