"""
Client Receptionist Agent — WhatsApp/Web-Rezeptionist für PILOTKUNDEN
(Elektrikerbetriebe im Pilotprogramm, siehe clients/README.md).

Unabhängig vom SDR-Agent (agents/sdr_agent.py, InboundChatGraph): der SDR-
Inbound-Chat beantwortet Fragen ÜBER NOVARA für Besucher von
novaraautomation.com und qualifiziert sie als Novara-Lead (ICP-Score,
Novara-Pakete/-Preise). DIESES Modul beantwortet Fragen FÜR EINEN
PILOTKUNDEN auf DESSEN eigener Website — ein Besucher, der einem
Elektrikerbetrieb schreibt, soll etwas über DIESEN Betrieb erfahren
(Leistungen, Notdienst, Termin), niemals über Novaras eigene Pakete. Beide
Chats teilen sich denselben Widget-Code (static/chat_widget.js, per
data-client-Attribut unterschieden) und dieselben Kern-Bausteine
(SecurityLayer-DLP, AI_DISCLOSURE_DE, core.lead_capture), aber bewusst
NICHT die ICP-Scoring-Logik oder novara_wissen.txt — ein Pilotkunde hat
kein "ICP-Fit zu Novara", sondern eine eigene Kundenanfrage, die
beantwortet werden muss.

Bewusst ein einfacherer, 2-Node-Graph statt der 4 Nodes des SDR-Inbound-
Chats (kein document_node/appointment_node-Split): das Pilotprogramm
deckt nur den Anfragen-Starter-Funktionsumfang ab (Rezeption + Termin +
Notdienst-Filter), keine PDF-/Foto-Angebotsanalyse — die käme mit dem
Autonome-Betrieb-Paket, ist für den ersten Piloten explizit nicht in
Scope (siehe Pilot-Plan-Konversation).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field
from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from typing_extensions import TypedDict

from agents.guardian_agent import resilient_node
from agents.sdr_agent import AI_DISCLOSURE_DE
from core import lead_capture
from core.db import Base, SessionLocal, engine
from core.llm import build_llm, cached_system_message
from core.client_profiles import ClientProfile, load_client_profile

logger = logging.getLogger(__name__)

_MAX_HISTORY_TURNS = 12


# ── Session-Persistenz ───────────────────────────────────────────────────────

class _ClientChatSessionRow(Base):
    """Persistierter Gesprächsverlauf EINER (client_id, session_id)-Kombination.

    DB-gestützt von Anfang an (core/db.py), nicht In-Memory wie
    agents/sdr_agent.py's InboundChatSession -- letzteres war eine bewusste
    Altlast aus vor der Postgres-Migration (21.09.2026); ein NEUES Modul für
    ein Pilotprogramm mit echten, zahlenden Interessenten sollte nicht mit
    derselben Einschränkung starten.
    """

    __tablename__ = "client_chat_sessions"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contact_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    contact_phone: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    contact_email: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Base.metadata.create_all(bind=engine, tables=[_ClientChatSessionRow.__table__])


def _get_or_create_session(client_id: str, session_id: str) -> _ClientChatSessionRow:
    with SessionLocal() as session:
        row = session.get(_ClientChatSessionRow, (client_id, session_id))
        if row is None:
            now = datetime.now(timezone.utc)
            row = _ClientChatSessionRow(
                client_id=client_id,
                session_id=session_id,
                history=[],
                turn_count=0,
                contact_name="",
                contact_phone="",
                contact_email="",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
        # Innerhalb der session detachen, damit der Aufrufer die Werte
        # außerhalb des with-Blocks noch lesen kann (SQLAlchemy expired
        # sonst alle Attribute beim Session-Close).
        session.expunge(row)
        return row


def _save_session(row: _ClientChatSessionRow) -> None:
    row.updated_at = datetime.now(timezone.utc)
    with SessionLocal() as session:
        session.merge(row)
        session.commit()


# ── System-Prompt (pro Kunde dynamisch gebaut, kein Modul-Konstante) ────────

def _build_system_prompt(profile: ClientProfile) -> str:
    leistungen = "\n".join(f"- {l}" for l in profile.leistungen) or "- (keine Leistungen hinterlegt)"
    faq = "\n".join(f"F: {e.frage}\nA: {e.antwort}" for e in profile.haeufige_fragen) or "(keine hinterlegt)"
    preis_regel = (
        f"Preise dürfen genannt werden, aber NUR gemäß dieser Regel: {profile.preishinweise}"
        if profile.preise_oeffentlich and profile.preishinweise
        else "Nenne NIEMALS konkrete Preise oder Kostenschätzungen -- verweise stattdessen "
        "freundlich auf ein kurzes Gespräch oder einen Vor-Ort-Termin, in dem der Betrieb "
        "den Aufwand einschätzen kann."
    )
    notdienst = profile.notdienst_regel or (
        "Keine spezielle Notdienst-Regel hinterlegt -- bei erkennbar dringenden/gefährlichen "
        "Anliegen (z. B. Stromausfall, Kurzschluss, Brandgeruch) trotzdem vorsichtshalber auf "
        "schnelle, direkte Kontaktaufnahme statt auf einen normalen Termin hinweisen."
    )
    # Die Notdienst-Regel beschreibt WANN/WIE reagiert werden soll ("Telefonnummer
    # nennen"), enthält aber selbst keine Ziffern -- ohne diese explizite Zeile
    # weiß das Modell zwar, DASS es eine Nummer nennen soll, aber nicht WELCHE
    # (gefunden bei der ersten echten Notdienst-Testkonversation: Modell verwies
    # korrekt auf "die Notdienstnummer", ohne sie zu nennen, weil sie nirgends
    # im Prompt stand).
    notdienst_kontakt = (
        f"Notdienst-/Kontaktnummer, die du bei einem Notfall IMMER wörtlich nennst: {profile.telefonnummer}"
        if profile.telefonnummer
        else "Keine Telefonnummer hinterlegt -- bei einem Notfall auf den regulären Kontaktweg "
        "der Website verweisen, keine Nummer erfinden."
    )

    return f"""\
Du bist der digitale Rezeptionist von {profile.firmenname} ({profile.branche}) \
und beantwortest den Chat auf DESSEN Website/WhatsApp. Ein Kunde von \
{profile.firmenname} schreibt dir direkt -- du vertrittst NICHT Novara \
Automation, sondern ausschließlich {profile.firmenname} selbst.

ANREDE (auf Deutsch): IMMER "Sie"/"Ihnen"/"Ihr", NIEMALS "du"/"dir"/"dein" \
-- Ton: {profile.ton}. Bei Antworten auf Englisch entfällt diese Regel \
naturgemäß.

=== BETRIEBSDATEN (deine EINZIGE Quelle für Fakten über {profile.firmenname}) ===
Servicegebiet: {profile.servicegebiet or "nicht hinterlegt -- im Zweifel nachfragen"}
Leistungen:
{leistungen}

Häufig gestellte Fragen (nutze diese Antworten wortgetreu, wo passend):
{faq}

PREISE: {preis_regel}

NOTDIENST-REGEL: {notdienst}
{notdienst_kontakt}
=== ENDE BETRIEBSDATEN ===

DEINE AUFGABE (in JEDER Antwort):
1. Beantworte die Anfrage hilfreich und konkret, NUR mit Fakten aus den
   Betriebsdaten oben. Erfinde NIEMALS eine Leistung, einen Preis oder eine
   Zusage, die dort nicht steht -- verweise im Zweifel auf ein persönliches
   Gespräch mit {profile.firmenname}.
2. Erkenne, ob die Anfrage DRINGEND ist (siehe Notdienst-Regel oben) --
   setze "urgent": true, wenn ja, und befolge dabei exakt die
   Notdienst-Regel. Wenn diese verlangt, eine Telefonnummer zu nennen,
   schreibe die Ziffern WÖRTLICH in deine Antwort (siehe Notdienst-/
   Kontaktnummer oben) -- ein Verweis wie "unsere Notdienstnummer" ohne
   die eigentliche Nummer hilft dem Kunden im Notfall nicht.
3. Wenn der Kunde von sich aus Kontaktdaten nennt (Name, Telefon, E-Mail),
   bedanke dich kurz und nutze sie weiter -- erfinde nie einen Namen oder
   eine Nummer, die nicht genannt wurde.
4. Biete einen Termin an (should_offer_booking: true), sobald der Kunde ein
   konkretes, nicht-dringendes Anliegen genannt hat UND noch kein Termin im
   Gespräch angeboten wurde. Bei einer dringenden Notdienst-Anfrage NIE
   einen Termin anbieten -- da zählt der direkte Kontakt (siehe
   Notdienst-Regel).

Gib AUSSCHLIESSLICH valides JSON zurück (kein Text davor/danach):
{{
  "reply": string (deine Chat-Antwort, 2-4 Sätze, in der Sprache des Kunden,
                   kein Technik-Jargon; KEINE geraden Anführungszeichen "
                   im Text -- sie zerstören das JSON),
  "urgent": boolean,
  "contact_name": string (NUR wenn tatsächlich genannt, sonst ""),
  "contact_phone": string (NUR wenn tatsächlich genannt, sonst ""),
  "contact_email": string (NUR wenn tatsächlich genannt, sonst ""),
  "should_offer_booking": boolean,
  "language": "de" | "en"
}}
"""


# ── JSON-Parsing (bewusst dupliziert aus agents/sdr_agent.py, siehe dessen
# Moduldocstring-Konvention: dieses Modul bleibt unabhängig, kein Import
# quer über Agent-Dateien hinweg) ───────────────────────────────────────────

def _extract_balanced_json_object(text: str) -> Optional[str]:
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
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped


_JSON_KEYS = "urgent|contact_name|contact_phone|contact_email|should_offer_booking|language"


def _salvage_broken_json(text: str) -> dict:
    """Rettet zumindest das reply-Feld aus ungültigem JSON (z. B. wegen eines
    geraden " im Antworttext) -- siehe agents/sdr_agent.py._salvage_broken_json,
    derselbe reale Bug (2026-09-26 in Produktion gefunden), hier präventiv
    von Anfang an mit übernommen."""
    body = _strip_markdown_fence(text)
    m = re.search(r'"reply"\s*:\s*"', body)
    if not m:
        return {}
    rest = body[m.end():]
    end = re.search(rf'"\s*,\s*"(?:{_JSON_KEYS})"\s*:', rest)
    reply_raw = rest[: end.start()] if end else rest.rstrip().rstrip("}").rstrip().rstrip('"')
    reply = reply_raw.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\").strip()
    if not reply:
        return {}
    out: dict = {"reply": reply}
    urgent = re.search(r'"urgent"\s*:\s*(true|false)', body)
    if urgent:
        out["urgent"] = urgent.group(1) == "true"
    lang = re.search(r'"language"\s*:\s*"([^"\n]*)"', body)
    if lang:
        out["language"] = lang.group(1)
    return out


def _looks_like_json(text: str) -> bool:
    body = _strip_markdown_fence(text or "")
    return body.startswith("{") or '"reply"' in body


def _parse_llm_json(text: str) -> dict:
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


# ── Graph State ──────────────────────────────────────────────────────────────

class ClientChatState(TypedDict):
    client_id: str
    session_id: str
    message: str
    visitor_info: dict[str, Any]

    history: list[dict[str, str]]
    is_first_turn: bool

    reply_text: str
    urgent: bool
    contact_name: str
    contact_phone: str
    contact_email: str
    should_offer_booking: bool
    language: str

    final_result: dict[str, Any]


_FALLBACK_REPLY = (
    "Entschuldigung, da ist gerade technisch etwas schiefgelaufen — "
    "möchten Sie Ihre Frage nochmal stellen?"
)


def _defensive_final_result(state: ClientChatState, profile: ClientProfile) -> dict[str, Any]:
    def _safe_str(value: Any, default: str = "") -> str:
        return value if isinstance(value, str) else default

    should_book = bool(state.get("should_offer_booking")) and not state.get("urgent")
    return {
        "reply": _safe_str(state.get("reply_text"), _FALLBACK_REPLY),
        "urgent": bool(state.get("urgent")),
        "should_offer_booking": should_book,
        "booking_url": profile.buchungslink if should_book and profile.buchungslink else None,
        "language": _safe_str(state.get("language"), "de"),
    }


def _client_chat_fallback(state: dict[str, Any]) -> dict[str, Any]:
    """fallback_builder für @resilient_node -- garantiert IMMER ein gültiges
    final_result, auch wenn ein Node trotz Retries endgültig fehlschlägt."""
    if state.get("final_result"):
        return state
    reply = state.get("reply_text") or _FALLBACK_REPLY
    state = {**state, "reply_text": reply}
    profile = state.pop("_profile", None)
    final_result = {
        "reply": reply,
        "urgent": bool(state.get("urgent")),
        "should_offer_booking": False,
        "booking_url": None,
        "language": state.get("language") or "de",
    }
    return {**state, "final_result": final_result}


class ClientReceptionistGraph:
    """2-Node-Graph: respond_node (LLM-Antwort) → finalize_node (Offenlegung + Lead-Capture)."""

    def __init__(self, profile: ClientProfile) -> None:
        self.profile = profile
        self._llm = build_llm(max_tokens=512)
        self._system_prompt = _build_system_prompt(profile)
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(ClientChatState)
        workflow.add_node("respond_node", self.respond_node)
        workflow.add_node("finalize_node", self.finalize_node)
        workflow.set_entry_point("respond_node")
        workflow.add_edge("respond_node", "finalize_node")
        workflow.add_edge("finalize_node", END)
        return workflow.compile()

    @resilient_node(fallback_builder=_client_chat_fallback)
    def respond_node(self, state: ClientChatState) -> ClientChatState:
        logger.info(
            "Node: respond_node", extra={"client": state["client_id"], "session": state["session_id"]}
        )
        messages: list = [cached_system_message(self._system_prompt)]
        for turn in state["history"]:
            msg_cls = HumanMessage if turn.get("role") == "user" else AIMessage
            messages.append(msg_cls(content=turn.get("content", "")))
        visitor_note = ""
        known = {k: v for k, v in (state.get("visitor_info") or {}).items() if v}
        if known:
            visitor_note = f"[Bekannte Besucherdaten: {json.dumps(known, ensure_ascii=False)}]\n"
        messages.append(HumanMessage(content=f"{visitor_note}{state['message']}"))

        try:
            response = self._llm.invoke(messages)
        except Exception as exc:
            logger.warning("respond_node: LLM-Aufruf fehlgeschlagen: %s", exc)
            return {**state, "reply_text": _FALLBACK_REPLY}

        content = response.content if isinstance(response.content, str) else ""
        try:
            data = _parse_llm_json(content)
        except Exception as exc:
            logger.warning("respond_node: LLM-Antwort war kein valides JSON: %s", exc)
            salvaged = _salvage_broken_json(content)
            if salvaged:
                return {
                    **state,
                    "reply_text": salvaged["reply"],
                    "urgent": salvaged.get("urgent", state.get("urgent", False)),
                    "language": salvaged.get("language") or state.get("language", "de"),
                }
            if _looks_like_json(content):
                return {**state, "reply_text": _FALLBACK_REPLY}
            raw_reply = _strip_markdown_fence(content)
            return {**state, "reply_text": raw_reply or _FALLBACK_REPLY}

        return {
            **state,
            "reply_text": data.get("reply") or _FALLBACK_REPLY,
            "urgent": bool(data.get("urgent", False)),
            "contact_name": data.get("contact_name") or state.get("contact_name", ""),
            "contact_phone": data.get("contact_phone") or state.get("contact_phone", ""),
            "contact_email": data.get("contact_email") or state.get("contact_email", ""),
            "should_offer_booking": bool(data.get("should_offer_booking", False)),
            "language": data.get("language") or state.get("language", "de"),
        }

    @resilient_node(fallback_builder=_client_chat_fallback)
    def finalize_node(self, state: ClientChatState) -> ClientChatState:
        reply = state["reply_text"]
        if state.get("is_first_turn"):
            disclosure = AI_DISCLOSURE_DE.format(client_name=self.profile.firmenname)
            if disclosure not in reply:
                reply = f"{reply}\n\n{disclosure}"

        # Lead-Capture -- eigener source-Namensraum pro Kunde, damit ein Pilot
        # nicht mit Novaras eigenen Leads oder einem anderen Piloten kollidiert.
        try:
            contact_fields = {
                "name": state.get("contact_name", ""),
                "email": state.get("contact_email", ""),
                "phone": state.get("contact_phone", ""),
            }
            new_lead = lead_capture.capture(
                source=f"client:{state['client_id']}",
                session_id=state["session_id"],
                message=state["message"],
                name=contact_fields["name"],
                email=contact_fields["email"],
                phone=contact_fields["phone"],
                company=self.profile.firmenname,
            )
            if new_lead:
                logger.info(
                    "Lead erfasst (Pilotkunde)",
                    extra={"client": state["client_id"], "session": state["session_id"]},
                )
        except Exception:
            logger.exception("finalize_node: Lead-Capture fehlgeschlagen")

        new_state = {**state, "reply_text": reply}
        final_result = _defensive_final_result(new_state, self.profile)
        return {**new_state, "final_result": final_result}

    def run(self, session_id: str, message: str, visitor_info: dict[str, Any]) -> dict[str, Any]:
        row = _get_or_create_session(self.profile.client_id, session_id)
        history = list(row.history or [])
        is_first_turn = row.turn_count == 0

        initial_state: ClientChatState = {
            "client_id": self.profile.client_id,
            "session_id": session_id,
            "message": message,
            "visitor_info": visitor_info,
            "history": history[-_MAX_HISTORY_TURNS:],
            "is_first_turn": is_first_turn,
            "reply_text": "",
            "urgent": False,
            "contact_name": row.contact_name,
            "contact_phone": row.contact_phone,
            "contact_email": row.contact_email,
            "should_offer_booking": False,
            "language": "de",
            "final_result": {},
        }
        result_state = self.graph.invoke(initial_state)

        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": result_state["final_result"].get("reply", "")})
        row.history = history[-(_MAX_HISTORY_TURNS * 2):]
        row.turn_count += 1
        row.contact_name = result_state.get("contact_name") or row.contact_name
        row.contact_phone = result_state.get("contact_phone") or row.contact_phone
        row.contact_email = result_state.get("contact_email") or row.contact_email
        _save_session(row)

        return result_state["final_result"]


class ClientReceptionistAgent:
    """Dünner Wrapper -- lädt das Betriebsprofil einmal, hält den kompilierten Graph."""

    def __init__(self, client_id: str) -> None:
        self.profile = load_client_profile(client_id)
        self._graph = ClientReceptionistGraph(self.profile)

    def process_chat(self, session_id: str, message: str, visitor_info: dict[str, Any]) -> dict[str, Any]:
        return self._graph.run(session_id, message, visitor_info)
