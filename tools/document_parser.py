"""
Document Parser Tool.
Unterstützt: Plain-Text und PDF (via pdfplumber).
Für den Operations Agent: extrahiert strukturierte Felder aus Rechnungstexten.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ParsedDocument(dict):
    """Dict-Subklasse mit typisiertem Zugriff auf Extraktionsergebnisse."""

    @property
    def company_name(self) -> str | None:
        return self.get("company_name")

    @property
    def amount(self) -> float | None:
        return self.get("amount")

    @property
    def currency(self) -> str:
        return self.get("currency", "EUR")

    @property
    def invoice_date(self) -> str | None:
        return self.get("invoice_date")

    @property
    def invoice_number(self) -> str | None:
        return self.get("invoice_number")

    @property
    def text_hash(self) -> str | None:
        return self.get("text_hash")


class DocumentParser:
    """
    Extraktion strukturierter Felder aus Rechnungstexten.
    Strategie: Regex-Heuristiken zuerst (schnell, deterministisch),
    LLM-Fallback im OperationsAgent für unklare Felder.
    """

    # --- Regex Patterns (deutsche & englische Rechnungen) ---

    _AMOUNT_PATTERN = re.compile(
        r"(?:gesamt|total|betrag|summe|netto|brutto|amount)[^\d€$]*"
        r"([\d.,]+)\s*([€$£]|EUR|USD|GBP)?",
        re.IGNORECASE,
    )
    _AMOUNT_FALLBACK = re.compile(
        r"([\d]{1,6}[.,]\d{2})\s*([€$£]|EUR|USD|GBP)",
        re.IGNORECASE,
    )
    _DATE_PATTERN = re.compile(
        r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2})\b"
    )
    _INVOICE_NR_PATTERN = re.compile(
        r"(?:rechnungs?[-.]?(?:nr\.?|nummer)|invoice\s*(?:no\.?|number))\s*[:#]\s*([A-Z0-9][A-Z0-9\-/]{2,})",
        re.IGNORECASE,
    )
    _COMPANY_PATTERNS = [
        re.compile(r"(?:von|from|absender|sender)[:\s]+([A-ZÄÖÜ][^\n,]{2,50})", re.IGNORECASE),
        re.compile(r"(?:firma|company|lieferant|vendor)[:\s]+([A-ZÄÖÜ][^\n,]{2,50})", re.IGNORECASE),
        re.compile(r"^([A-ZÄÖÜ][A-Za-zäöüÄÖÜß\s&\-\.]{3,50}(?:GmbH|AG|KG|GbR|Ltd|Inc|UG|SE))",
                   re.MULTILINE),
    ]
    _CURRENCY_MAP = {"€": "EUR", "$": "USD", "£": "GBP"}

    def parse_text(self, text: str) -> ParsedDocument:
        """Extrahiert strukturierte Felder aus einem Rechnungstext."""
        result: dict[str, Any] = {
            "text_hash": self._hash(text),
            "raw_length": len(text),
        }

        result["company_name"] = self._extract_company(text)
        result["amount"], result["currency"] = self._extract_amount(text)
        result["invoice_date"] = self._extract_date(text)
        result["invoice_number"] = self._extract_invoice_number(text)

        logger.debug("Document parsed", extra={"fields_found": [k for k, v in result.items() if v]})
        return ParsedDocument(result)

    def extract_text_from_pdf(self, pdf_bytes: bytes) -> str:
        """Extract raw text from PDF bytes without parsing to structured fields."""
        try:
            import pdfplumber
        except ImportError:
            raise RuntimeError("pdfplumber not installed. Run: pip install pdfplumber")

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page_count = len(pdf.pages)
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        logger.info("PDF text extracted", extra={"pages": page_count, "chars": len(full_text)})
        return full_text

    def parse_pdf(self, pdf_bytes: bytes) -> ParsedDocument:
        """Extrahiert Text aus einem PDF und parst ihn."""
        full_text = self.extract_text_from_pdf(pdf_bytes)
        return self.parse_text(full_text)

    def parse_file(self, path: str | Path) -> ParsedDocument:
        p = Path(path)
        if p.suffix.lower() == ".pdf":
            return self.parse_pdf(p.read_bytes())
        return self.parse_text(p.read_text(encoding="utf-8"))

    # --- Private helpers ---

    def _extract_amount(self, text: str) -> tuple[float | None, str]:
        for pattern in (self._AMOUNT_PATTERN, self._AMOUNT_FALLBACK):
            match = pattern.search(text)
            if match:
                amount = self._parse_amount(match.group(1))
                if amount is None:
                    continue
                currency_raw = match.group(2) or "EUR"
                currency = self._CURRENCY_MAP.get(currency_raw.strip(), currency_raw.upper())
                return amount, currency
        return None, "EUR"

    @staticmethod
    def _parse_amount(raw: str) -> float | None:
        """
        Parse a monetary string in either German (1.234,56) or English/US
        (1,234.56) format.

        Both separators present → the rightmost one is the decimal separator.
        Only one separator present → it's the decimal separator if one or
        two digits follow it (e.g. "1234,5" / "1234,56" / "1234.5" /
        "1234.56"), otherwise it's a thousands separator with no cents
        (e.g. "1,234" / "1.234" → 1234).
        """
        raw = raw.strip()
        has_comma, has_dot = "," in raw, "." in raw
        if has_comma and has_dot:
            cleaned = (raw.replace(".", "").replace(",", ".")
                       if raw.rfind(",") > raw.rfind(".")
                       else raw.replace(",", ""))
        elif has_comma:
            cleaned = (raw.replace(",", ".") if len(raw.split(",")[-1]) in (1, 2)
                       else raw.replace(",", ""))
        elif has_dot:
            cleaned = (raw if len(raw.split(".")[-1]) in (1, 2)
                       else raw.replace(".", ""))
        else:
            cleaned = raw
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _extract_date(self, text: str) -> str | None:
        match = self._DATE_PATTERN.search(text)
        return match.group(1) if match else None

    def _extract_invoice_number(self, text: str) -> str | None:
        match = self._INVOICE_NR_PATTERN.search(text)
        return match.group(1).strip() if match else None

    def _extract_company(self, text: str) -> str | None:
        for pattern in self._COMPANY_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1).strip()
        return None

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]
