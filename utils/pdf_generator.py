"""
PDF-Generator für den Regiebericht (Baustellen-Voice-Assistant über WhatsApp).

Ein Regiebericht ist im DACH-Bauhandwerk das Standarddokument, mit dem ein
Techniker/Handwerker seine auf der Baustelle geleisteten Stunden, verwendetes
Material und die durchgeführte Tätigkeit gegenüber dem Kunden/Auftraggeber
dokumentiert — Grundlage für Verrechnung/Regie-Abrechnung. Dieses Modul
formatiert die von agents/field_worker_agent.py aus einer WhatsApp-Nachricht
extrahierten Daten in ein professionelles PDF.

Bewusst unter utils/ statt tools/: tools/ enthält in diesem Repo ausschließlich
Business-Tools mit eigenem Domänenzustand (Mock-CRM, Ticket-System, Lead-DB,
...), die von genau einem Agenten-Graphen verwendet werden. Dieses Modul ist
reine, zustandslose Formatierungslogik (Daten rein, PDF-Datei raus) ohne jede
Geschäftslogik — näher an core/security.py (reine Funktionen) als an einem
Business-Tool, aber themenfremd genug (Dokumentenerzeugung statt
Sicherheits-/Konfigurations-Infrastruktur), um ein eigenes, generisches
utils/-Paket zu rechtfertigen statt es in core/ zu pressen.

fpdf2s Core-Fonts (Helvetica/Times/Courier, keine gebündelte TTF-Datei nötig)
verwenden intern eine WinAnsi-ähnliche Kodierung, die alle deutschen Umlaute
(ä/ö/ü/Ä/Ö/Ü/ß) UND das Euro-Zeichen (€) nativ unterstützt — für reinen
Regiebericht-Text (kein Chinesisch/Kyrillisch/Emoji) reicht das aus, ohne eine
Unicode-TTF-Schriftdatei ins Repo aufnehmen zu müssen.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Union

from fpdf import FPDF
from fpdf.enums import XPos, YPos

logger = logging.getLogger(__name__)

_TITLE = "Regiebericht"
_BRAND_BLUE = (0, 102, 255)   # Novara-Blau, siehe website/css/style.css --blue
_MUTED_GREY = (110, 110, 110)
_TEXT_DARK = (20, 20, 20)
_DEMO_RED = (200, 0, 0)
_DEMO_WATERMARK_TEXT = "Novara Automation - DEMO"


class _RegieberichtPDF(FPDF):
    """Eigene Kopf-/Fußzeile — sonst identisch zu FPDF.

    is_demo=True (Demo-Sandbox, tools/demo_sandbox.py) fügt einen deutlich
    sichtbaren "DEMO TEST"-Vermerk in Kopf-, Fuß- und als diagonalen
    Wasserzeichen-Text auf jeder Seite hinzu — ein Demo-Regiebericht darf mit
    einem echten NIE verwechselbar sein.
    """

    def __init__(self, *args: Any, is_demo: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._is_demo = is_demo

    def header(self) -> None:  # noqa: D102 -- fpdf2-Hook, keine eigene Doku nötig
        self.set_font("helvetica", "B", 18)
        self.set_text_color(*_BRAND_BLUE)
        self.cell(0, 12, _TITLE, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("helvetica", "", 9)
        self.set_text_color(*_MUTED_GREY)
        # ASCII-Bindestrich statt Halbgeviertstrich (–) -- fpdf2s
        # Core-Fonts nutzen echtes ISO-8859-1 (0-255), nicht das erweiterte
        # Windows-1252-Repertoire, das den Halbgeviertstrich enthält. Traf
        # genau diese Zeile beim ersten Smoke-Test (FPDFUnicodeEncodingException).
        self.cell(0, 6, "Novara Automation - Baustellen-Voice-Assistant", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if self._is_demo:
            self.set_font("helvetica", "B", 11)
            self.set_text_color(*_DEMO_RED)
            self.cell(0, 7, _clean_text(_DEMO_WATERMARK_TEXT), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
            self._draw_demo_diagonal_watermark()
        self.set_draw_color(200, 200, 200)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.get_y() + 2, self.w - self.r_margin, self.get_y() + 2)
        self.ln(10)

    def _draw_demo_diagonal_watermark(self) -> None:
        """Blasser, diagonaler Wasserzeichen-Text über der Seitenmitte --
        rein optisch, wird von rotation() automatisch nach dem with-Block
        zurückgesetzt, beeinflusst also die restliche Seitenlogik nicht."""
        self.set_font("helvetica", "B", 46)
        self.set_text_color(235, 210, 210)
        with self.rotation(45, x=self.w / 2, y=self.h / 2):
            self.text(self.w / 2 - 95, self.h / 2, _clean_text(_DEMO_WATERMARK_TEXT))
        self.set_text_color(*_MUTED_GREY)

    def footer(self) -> None:  # noqa: D102
        self.set_y(-15)
        self.set_font("helvetica", "I", 8)
        self.set_text_color(*_MUTED_GREY)
        footer_text = f"Seite {self.page_no()}/{{nb}}"
        if self._is_demo:
            footer_text += "  |  " + _clean_text(_DEMO_WATERMARK_TEXT) + " - keine echten Daten"
        self.cell(0, 10, footer_text, align="C")


# fpdf2s Core-Fonts kodieren mit ECHTEM ISO-8859-1 (0-255), NICHT dem
# erweiterten Windows-1252-Repertoire -- das Euro-Zeichen (€, U+20AC) und
# "smarte" Typografiezeichen (–/—/‘’/“”/…) liegen AUSSERHALB dieses Bereichs
# und werfen sonst FPDFUnicodeEncodingException. Verifiziert per Smoke-Test
# (beide Zeichen crashten zunächst trotz gegenteiliger Annahme im ersten
# Entwurf dieses Moduls) -- gezielte, lesbare Ersetzungen statt eines
# pauschalen "?" für die Zeichen, die in von field_worker_agent.py
# LLM-generiertem Text plausibel vorkommen (€-Beträge, Gedankenstriche,
# typografische Anführungszeichen aus dem Modell-Output).
_CHAR_SUBSTITUTIONS: dict[str, str] = {
    "€": "EUR",   # €
    "–": "-",     # – (Halbgeviertstrich)
    "—": "-",     # — (Geviertstrich)
    "‘": "'", "’": "'",   # ‘ ’
    "“": '"', "”": '"',   # “ ”
    "…": "...",   # …
}


def _clean_text(value: Any) -> str:
    """
    Best-Effort-Bereinigung für fpdf2s Core-Font-Zeichensatz: NFKC-Normalisierung
    (z. B. Kombinationszeichen -> vorkomponierte Umlaute), gezielte Ersetzungen
    für gängige Zeichen außerhalb von Latin-1 (siehe _CHAR_SUBSTITUTIONS), und
    "?" als letzter Ausweg für alles andere (z. B. Emoji, kyrillische/asiatische
    Zeichen aus Spracherkennung/Autokorrektur) statt eine
    FPDFUnicodeEncodingException zu riskieren. Kein Datenverlust bei normalem
    österreichischem Deutsch (Umlaute, ß bleiben erhalten, sie liegen innerhalb
    von Latin-1).
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    out_chars = []
    for ch in text:
        if ord(ch) < 256:
            out_chars.append(ch)
        elif ch in _CHAR_SUBSTITUTIONS:
            out_chars.append(_CHAR_SUBSTITUTIONS[ch])
        else:
            out_chars.append("?")
    return "".join(out_chars)


def _safe_filename_component(value: str, max_len: int = 40) -> str:
    """Reduziert einen String auf sichere Dateinamen-Zeichen (a-zA-Z0-9_-)."""
    ascii_only = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", ascii_only).strip("_")
    return (cleaned or "Unbekannt")[:max_len]


def _write_field(pdf: FPDF, label: str, value: str) -> None:
    pdf.set_font("helvetica", "B", 10.5)
    pdf.set_text_color(*_MUTED_GREY)
    pdf.cell(50, 7, _clean_text(label), new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.set_font("helvetica", "", 11)
    pdf.set_text_color(*_TEXT_DARK)
    pdf.cell(0, 7, _clean_text(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def suggested_filename(data: dict[str, Any], is_demo: bool = False) -> str:
    """
    Baut einen eindeutigen, sicheren Dateinamen aus Techniker+Kunde+Zeitstempel
    -- reiner Vorschlag für Aufrufer, die mehrere Regieberichte parallel/
    nacheinander erzeugen (z. B. main.py POST /api/v1/webhook/whatsapp, wo ein
    fixer "Regiebericht.pdf"-Name gleichzeitige Anfragen überschreiben würde).
    generate_regiebericht() selbst erzwingt das NICHT -- ihr Default-Dateiname
    ist bewusst exakt "Regiebericht.pdf" (siehe deren Docstring).
    """
    technician = _safe_filename_component(str(data.get("techniker") or "Techniker"))
    customer = _safe_filename_component(str(data.get("kunde") or "Kunde"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    prefix = "DEMO_" if is_demo else ""
    return f"{prefix}Regiebericht_{technician}_{customer}_{stamp}.pdf"


def generate_regiebericht(
    data: dict[str, Any],
    output_path: Union[str, Path] = "Regiebericht.pdf",
    is_demo: bool = False,
) -> Path:
    """
    Erzeugt einen professionellen, deutschsprachigen Regiebericht als PDF.

    `data` erwartet (alle Felder optional, fehlende werden als "-" dargestellt):
      - techniker: str            -- Name des Technikers/Handwerkers
      - kunde: str                -- Kunde bzw. Baustelle/Projekt
      - datum: str                -- Default: heutiges Datum (Europe/Vienna-unabhängig, UTC-Datum)
      - stunden: float | int | str -- geleistete Stunden
      - material: str | list[str] -- verwendetes Material (Liste wird mit ", " verbunden)
      - arbeit: str                -- Beschreibung der durchgeführten Tätigkeit

    `is_demo=True` (Demo-Sandbox, tools/demo_sandbox.py) blendet einen
    deutlich sichtbaren "Novara Automation - DEMO TEST"-Vermerk in Kopf-,
    Fuß- und als diagonales Wasserzeichen ein, damit ein Demo-Regiebericht
    nie mit einem echten verwechselt werden kann.

    `output_path` ist die vollständige Zieldatei (Ordner werden bei Bedarf
    angelegt) -- Default ist wörtlich "Regiebericht.pdf" im aktuellen
    Arbeitsverzeichnis. Aufrufer mit mehreren/parallelen Berichten (z. B. der
    WhatsApp-Webhook) sollten einen eindeutigen Pfad übergeben, z. B. über
    `suggested_filename()` oben, statt sich gegenseitig zu überschreiben.

    Gibt den Pfad der geschriebenen Datei zurück. Wirft bei einem I/O-Fehler
    (z. B. nicht beschreibbares Verzeichnis) — bewusst KEIN eigenes
    try/except hier: der Aufrufer (main.py) entscheidet, wie ein
    fehlgeschlagener PDF-Export dem WhatsApp-Nutzer gegenüber kommuniziert
    wird, ein stiller Fallback hier würde das verschleiern.
    """
    pdf = _RegieberichtPDF(format="A4", unit="mm", is_demo=is_demo)
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.set_margins(left=20, top=20, right=20)
    pdf.add_page()

    datum = data.get("datum") or datetime.now(timezone.utc).strftime("%d.%m.%Y")
    stunden = data.get("stunden")
    stunden_text = f"{stunden}" if stunden not in (None, "") else "-"

    material = data.get("material")
    if isinstance(material, (list, tuple)):
        material_text = ", ".join(str(m) for m in material if str(m).strip()) or "-"
    else:
        material_text = str(material).strip() or "-" if material else "-"

    _write_field(pdf, "Datum:", datum)
    _write_field(pdf, "Techniker:", data.get("techniker") or "-")
    _write_field(pdf, "Kunde / Baustelle:", data.get("kunde") or "-")
    _write_field(pdf, "Geleistete Stunden:", stunden_text)
    _write_field(pdf, "Verwendetes Material:", material_text)

    pdf.ln(4)
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(*_TEXT_DARK)
    pdf.cell(0, 8, _clean_text("Durchgeführte Arbeiten"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_draw_color(220, 220, 220)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)

    pdf.set_font("helvetica", "", 11)
    arbeit_text = _clean_text(data.get("arbeit") or "-")
    pdf.multi_cell(0, 6.5, arbeit_text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(10)
    pdf.set_font("helvetica", "I", 8.5)
    pdf.set_text_color(*_MUTED_GREY)
    pdf.multi_cell(
        0, 5,
        _clean_text(
            "Dieser Regiebericht wurde automatisch aus einer WhatsApp-Sprach- oder "
            "Textnachricht erstellt (Novara Automation Baustellen-Voice-Assistant) "
            "und dient als Grundlage für die Leistungsabrechnung."
        ),
    )

    out_path = Path(output_path)
    if out_path.parent != Path("."):
        out_path.parent.mkdir(parents=True, exist_ok=True)

    pdf.output(str(out_path))
    logger.info("Regiebericht erstellt", extra={"path": str(out_path)})
    return out_path
