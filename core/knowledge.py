"""Lädt Mandanten-Wissensdatenbanken einmalig beim Start und stellt sie den Agenten zur Verfügung.

Bisher war hier nur Novaras eigenes `novara_wissen.txt` fest verdrahtet. Für den
Einsatz bei anderen Kunden (z. B. DKH) muss ein Agent stattdessen die
Wissensdatei DIESES Kunden laden können — `load_wissen()` nimmt dafür einen
Mandanten-Kurznamen (siehe `_KNOWN_CLIENTS`) oder einen beliebigen Dateipfad
entgegen, statt den Dateinamen im Code festzuschreiben.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Kurznamen für Wissensdateien, die im Projektroot von novara-agents liegen.
# Ein Kunde ohne Eintrag hier kann trotzdem geladen werden — dann wird `client`
# direkt als Dateiname/Pfad interpretiert (siehe load_wissen()).
_KNOWN_CLIENTS: dict[str, str] = {
    "novara": "novara_wissen.txt",
    # DKH (Deutsches Kulturhaus e.V., Passau) — Pilotkunde. Deckt beide
    # Zielgruppen-Seiten der Website ab (deutsche Hauptseite für
    # Institutionen/Kommunen + spanische /ausbildung-Landingpage fürs
    # Lateinamerika-Programm), da beide von derselben Wissensdatei bedient
    # werden. Liegt bewusst außerhalb dieses Repos, im Schwesterprojekt.
    "dkh": "/Users/antonpfortevelazquez/kulturhaus-website/historia_kulturhaus.txt",
    # Institut für Berufsstrategie — eigenständiges Einzelunternehmen, teilt
    # sich aber Kontakt-Domain/Infrastruktur mit DKH. Eigene Wissensdatei,
    # weil Zielgruppen und Angebote inhaltlich anders sind als beim
    # allgemeinen DKH-Wissen.
    "berufsstrategie": "/Users/antonpfortevelazquez/kulturhaus-website/institut_berufsstrategie_wissen.txt",
}


@lru_cache(maxsize=None)
def load_wissen(client: str = "novara") -> str:
    """Lädt die Wissensdatenbank eines Mandanten einmalig und cached sie pro Mandant.

    `client` ist entweder ein bekannter Kurzname aus `_KNOWN_CLIENTS` oder ein
    Dateiname/absoluter Pfad zu einer beliebigen Wissensdatei (z. B. das
    Kunden-Wissen eines anderen Projekts wie DKH, das nicht im Repo von
    novara-agents liegt). Relative Pfade werden gegen den Projektroot von
    novara-agents aufgelöst.
    """
    target = _KNOWN_CLIENTS.get(client, client)
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = _ROOT / path
    try:
        # errors="replace" verhindert UnicodeDecodeError bei kaputten Bytes
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"(Wissensdatenbank für Mandant '{client}' nicht gefunden unter {path} — Pfad prüfen)"


def load_novara_wissen() -> str:
    """Rückwärtskompatibler Alias — lädt weiterhin Novaras eigenes Wissen.

    Bestehende Agenten-Importe (`from core.knowledge import load_novara_wissen`)
    funktionieren dadurch unverändert weiter.
    """
    return load_wissen("novara")
