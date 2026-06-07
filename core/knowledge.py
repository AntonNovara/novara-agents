"""Lädt die Novara-Wissensdatenbank einmalig beim Start und stellt sie allen Agenten zur Verfügung."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def load_novara_wissen() -> str:
    """Liest novara_wissen.txt einmalig ein und cached das Ergebnis."""
    path = _ROOT / "novara_wissen.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "(Wissensdatenbank nicht gefunden — bitte novara_wissen.txt im Projektverzeichnis ablegen)"
