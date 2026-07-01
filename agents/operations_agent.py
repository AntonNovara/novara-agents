"""
Operations Agent – MVP (vollständig implementiert).

Workflow (LangGraph StateGraph):
  classify_document
       ↓
  extract_fields          ← DocumentParser (Regex) + LLM-Fallback
       ↓
  validate_extraction
       ↓
  write_to_crm            ← CRMIntegration.upsert_invoice()
       ↓
  finalize

Wenn classify_document keinen Rechnungstyp erkennt, endet der Graph
in einem „unsupported_document"-Zustand ohne CRM-Schreibzugriff.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import JsonOutputParser
from langgraph.graph import END, StateGraph
from pydantic import BaseModel
from typing_extensions import TypedDict

from agents.base_agent import AgentRequest, BaseAgent
from core.config import settings
from core.knowledge import load_novara_wissen
from tools.crm_integration import CRMIntegration, ERPRecord
from tools.document_parser import DocumentParser, ParsedDocument

logger = logging.getLogger(__name__)

_WISSEN = load_novara_wissen()

# ── LLM singleton (lazy-init) ────────────────────────────────────────────────

def _parse_llm_json(text: str) -> dict:
    """Parse JSON from LLM output, stripping markdown code fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return json.loads(text.strip())


def _build_llm() -> ChatAnthropic:
    return ChatAnthropic(
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key.get_secret_value(),
        temperature=0,
        max_tokens=512,
    )


# ── Graph State ───────────────────────────────────────────────────────────────

class OperationsState(TypedDict):
    """Zentrales State-Objekt, das durch alle Graph-Knoten fließt."""
    input_text: str
    session_id: str

    # Populated by classify_document
    document_type: str          # "invoice" | "unknown"
    classification_confidence: float

    # Populated by extract_fields
    parsed: dict[str, Any]
    llm_enriched: bool

    # Populated by validate_extraction
    validation_passed: bool
    validation_errors: list[str]

    # Populated by write_to_crm
    crm_result: dict[str, Any]

    # Final output
    final_result: dict[str, Any]
    error: Optional[str]


# ── Extraction Schema for LLM ─────────────────────────────────────────────────

class InvoiceExtraction(BaseModel):
    company_name: Optional[str] = None
    amount: Optional[float] = None
    currency: str = "EUR"
    invoice_date: Optional[str] = None
    invoice_number: Optional[str] = None


_SYSTEM_EXTRACT = f"""\
Du bist ein Dokumentenverarbeitungs-Assistent bei Novara Automation (Wien, Österreich).
Novara stellt Rechnungen nach folgendem Schema aus (aus der Wissensdatenbank):
  - Rechnungsnummern: RE-2026-001, RE-2026-002, ... (sequentiell)
  - Kleinunternehmer — kein USt-Ausweis (§6 Abs. 1 Z 27 UStG)
  - Zahlungsmodell: 50% Anzahlung + 50% bei Übergabe
  - Pakete: Starter €990, Growth €2.490, Retainer €590/Monat

=== AUSZUG NOVARA WISSENSDATENBANK (Rechnungsstellung) ===
{_WISSEN}
=== ENDE ===

Extrahiere aus dem vorliegenden Text folgende Felder als valides JSON:
  - company_name: Name des ausstellenden Unternehmens
  - amount: Gesamtbetrag als Zahl (ohne Währungssymbol)
  - currency: ISO 4217 Code (EUR, USD, GBP ...)
  - invoice_date: Datum als ISO-Format (YYYY-MM-DD) oder DD.MM.YYYY
  - invoice_number: Rechnungs-/Dokumentnummer falls vorhanden, sonst null

Antworte NUR mit dem JSON-Objekt, keine Erklärung.
"""

_SYSTEM_CLASSIFY = """\
Klassifiziere das folgende Dokument in eine dieser Kategorien:
  - invoice: Eine Rechnung oder Faktura für Waren/Dienstleistungen
  - unknown: Alles andere

Antworte mit JSON: {"type": "<category>", "confidence": <0.0-1.0>}
"""


# ── Graph Nodes ───────────────────────────────────────────────────────────────

class OperationsGraph:
    """
    Kapselt den LangGraph-Workflow. Wird einmalig gebaut (_build_graph)
    und dann pro Request ausgeführt (run).
    """

    def __init__(self, llm: ChatAnthropic, parser: DocumentParser, crm: CRMIntegration) -> None:
        self._llm = llm
        self._parser = parser
        self._crm = crm
        self._graph = self._build_graph()

    # ── Node: classify_document ──────────────────────────────────────────────

    def classify_document(self, state: OperationsState) -> OperationsState:
        logger.info("Node: classify_document", extra={"session": state["session_id"]})

        # Fast path: heuristic keyword check before calling LLM
        text_lower = state["input_text"].lower()
        invoice_keywords = {"rechnung", "invoice", "betrag", "amount", "zahlung", "payment",
                            "eur", "usd", "mwst", "vat", "netto", "brutto"}
        keyword_hits = sum(1 for kw in invoice_keywords if kw in text_lower)

        if keyword_hits >= 2:
            logger.debug("classify_document: fast-path invoice detection")
            return {**state, "document_type": "invoice", "classification_confidence": 0.90}

        # LLM fallback for ambiguous documents
        try:
            response = self._llm.invoke([
                SystemMessage(content=_SYSTEM_CLASSIFY),
                HumanMessage(content=state["input_text"]),
            ])
            parsed = _parse_llm_json(response.content)
            doc_type = parsed.get("type", "unknown")
            confidence = float(parsed.get("confidence", 0.5))
        except Exception as exc:
            logger.warning("classify_document LLM call failed: %s", exc)
            doc_type, confidence = "unknown", 0.0

        return {**state, "document_type": doc_type, "classification_confidence": confidence}

    # ── Node: extract_fields ─────────────────────────────────────────────────

    def extract_fields(self, state: OperationsState) -> OperationsState:
        logger.info("Node: extract_fields", extra={"session": state["session_id"]})

        parsed: ParsedDocument = self._parser.parse_text(state["input_text"])
        llm_enriched = False

        # LLM enrichment only for fields the regex didn't catch
        missing_fields = [
            f for f in ("company_name", "amount", "invoice_date")
            if not parsed.get(f)
        ]

        if missing_fields:
            logger.debug("extract_fields: LLM enrichment for %s", missing_fields)
            try:
                response = self._llm.invoke([
                    SystemMessage(content=_SYSTEM_EXTRACT),
                    HumanMessage(content=state["input_text"]),
                ])
                llm_data: dict = _parse_llm_json(response.content)
                # Merge: only fill in gaps, don't override regex results
                for field in missing_fields:
                    if llm_data.get(field) and not parsed.get(field):
                        parsed[field] = llm_data[field]
                # Regex fallback for company_name if LLM also missed it
                if not parsed.get("company_name"):
                    m = re.search(r"(?:Firma|Unternehmen|Auftraggeber|Von)[:\s]+([^\n,]+)", state["input_text"], re.IGNORECASE)
                    if m:
                        parsed["company_name"] = m.group(1).strip()
                llm_enriched = True
            except Exception as exc:
                logger.warning("extract_fields LLM enrichment failed: %s", exc)

        return {**state, "parsed": dict(parsed), "llm_enriched": llm_enriched}

    # ── Node: validate_extraction ────────────────────────────────────────────

    def validate_extraction(self, state: OperationsState) -> OperationsState:
        logger.info("Node: validate_extraction", extra={"session": state["session_id"]})

        errors: list[str] = []
        p = state["parsed"]

        if not p.get("company_name"):
            errors.append("company_name is missing")
        if p.get("amount") is None:
            errors.append("amount is missing")
        elif p["amount"] <= 0:
            errors.append(f"amount must be positive, got {p['amount']}")
        if not p.get("invoice_date"):
            errors.append("invoice_date is missing")

        return {**state, "validation_passed": len(errors) == 0, "validation_errors": errors}

    # ── Node: write_to_crm ───────────────────────────────────────────────────

    def write_to_crm(self, state: OperationsState) -> OperationsState:
        logger.info("Node: write_to_crm", extra={"session": state["session_id"]})

        p = state["parsed"]
        record = ERPRecord(
            company_name=p.get("company_name", "Unknown"),
            amount=float(p.get("amount", 0)),
            currency=p.get("currency", "EUR"),
            invoice_date=p.get("invoice_date", ""),
            invoice_number=p.get("invoice_number"),
            raw_text_hash=p.get("text_hash"),
            source_agent="operations",
        )

        crm_result = self._crm.upsert_invoice(record)
        return {**state, "crm_result": crm_result.model_dump()}

    # ── Node: finalize ───────────────────────────────────────────────────────

    def finalize(self, state: OperationsState) -> OperationsState:
        logger.info("Node: finalize", extra={"session": state["session_id"]})

        final: dict[str, Any] = {
            "document_type": state["document_type"],
            "classification_confidence": state["classification_confidence"],
            "extracted_data": state.get("parsed", {}),
            "llm_enriched": state.get("llm_enriched", False),
            "validation": {
                "passed": state.get("validation_passed", False),
                "errors": state.get("validation_errors", []),
            },
            "crm": state.get("crm_result", {}),
        }

        return {**state, "final_result": final, "error": None}

    def finalize_unsupported(self, state: OperationsState) -> OperationsState:
        logger.info("Node: finalize_unsupported – not an invoice")
        return {
            **state,
            "final_result": {
                "document_type": state["document_type"],
                "classification_confidence": state["classification_confidence"],
                "message": "Document is not an invoice. No CRM action taken.",
            },
            "error": None,
        }

    def finalize_validation_failed(self, state: OperationsState) -> OperationsState:
        logger.warning("Node: finalize_validation_failed", extra={"errors": state.get("validation_errors")})
        return {
            **state,
            "final_result": {
                "document_type": state["document_type"],
                "extracted_data": state.get("parsed", {}),
                "validation_errors": state.get("validation_errors", []),
                "message": "Extraction incomplete – manual review required.",
            },
            "error": "Validation failed",
        }

    # ── Routing ──────────────────────────────────────────────────────────────

    @staticmethod
    def _route_after_classify(state: OperationsState) -> Literal["extract_fields", "finalize_unsupported"]:
        if state["document_type"] == "invoice":
            return "extract_fields"
        return "finalize_unsupported"

    @staticmethod
    def _route_after_validate(state: OperationsState) -> Literal["write_to_crm", "finalize_validation_failed"]:
        if state["validation_passed"]:
            return "write_to_crm"
        return "finalize_validation_failed"

    # ── Graph Builder ────────────────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(OperationsState)

        graph.add_node("classify_document", self.classify_document)
        graph.add_node("extract_fields", self.extract_fields)
        graph.add_node("validate_extraction", self.validate_extraction)
        graph.add_node("write_to_crm", self.write_to_crm)
        graph.add_node("finalize", self.finalize)
        graph.add_node("finalize_unsupported", self.finalize_unsupported)
        graph.add_node("finalize_validation_failed", self.finalize_validation_failed)

        graph.set_entry_point("classify_document")

        graph.add_conditional_edges(
            "classify_document",
            self._route_after_classify,
            {"extract_fields": "extract_fields", "finalize_unsupported": "finalize_unsupported"},
        )
        graph.add_edge("extract_fields", "validate_extraction")
        graph.add_conditional_edges(
            "validate_extraction",
            self._route_after_validate,
            {"write_to_crm": "write_to_crm", "finalize_validation_failed": "finalize_validation_failed"},
        )
        graph.add_edge("write_to_crm", "finalize")
        graph.add_edge("finalize", END)
        graph.add_edge("finalize_unsupported", END)
        graph.add_edge("finalize_validation_failed", END)

        return graph.compile()

    def run(self, input_text: str, session_id: str) -> dict[str, Any]:
        initial_state: OperationsState = {
            "input_text": input_text,
            "session_id": session_id,
            "document_type": "",
            "classification_confidence": 0.0,
            "parsed": {},
            "llm_enriched": False,
            "validation_passed": False,
            "validation_errors": [],
            "crm_result": {},
            "final_result": {},
            "error": None,
        }
        final_state = self._graph.invoke(initial_state)
        return final_state["final_result"]


# ── OperationsAgent ───────────────────────────────────────────────────────────

class OperationsAgent(BaseAgent):
    """
    Öffentliche Agent-Klasse. Erbt Security-Wrapper von BaseAgent.
    Delegiert die eigentliche Logik an OperationsGraph (LangGraph).
    """

    agent_type = "operations"

    def __init__(self) -> None:
        super().__init__()
        self._workflow = OperationsGraph(
            llm=_build_llm(),
            parser=DocumentParser(),
            crm=CRMIntegration(
                endpoint=settings.crm_endpoint,
                api_key=settings.crm_api_key.get_secret_value(),
            ),
        )

    def _run(self, request: AgentRequest) -> dict[str, Any]:
        return self._workflow.run(
            input_text=request.text,
            session_id=request.session_id,
        )
