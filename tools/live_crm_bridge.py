"""
Lokale Brücke zum Live-Produktions-CRM (Google Sheets) in
la-maquina-de-confianza/crm_handler.py.

NUR FÜR LOKALE ENTWICKLUNG. crm_handler.py ist kein installierbares Package,
sondern ein Skript im Schwester-Repo mit eigenem, an DIESEN Mac gebundenem
OAuth-Client (credentials.json) und -Token (token.json) — ein Railway-Deploy
von novara-agents hat auf keine dieser Dateien Zugriff. Aktiv nur, wenn
SDR_CRM_LIVE_SHEET=true gesetzt ist (core/config.py); sonst bleibt
CRMIntegrationSDR beim harmlosen In-Memory-Mock.

Siehe agents/sdr_agent.py (SDRAgent.__init__, vormals TODO "Swap auf echtes
CRM") und CLAUDE.md, Abschnitt "SDR Agent", für den Stand vor dieser Kopplung.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from core.config import settings

_crm_handler_module: Any = None  # lazy-imported crm_handler, cached nach erstem Erfolg


def _resolve_sibling_repo() -> Path:
    configured = Path(settings.la_maquina_de_confianza_path).expanduser()
    if not configured.is_absolute():
        configured = (Path(__file__).resolve().parent.parent / configured).resolve()
    return configured


def _load_crm_handler() -> Any:
    """
    Importiert crm_handler.py aus dem Schwester-Repo — mit vorab gesetzten
    ABSOLUTEN Pfaden, weil dessen eigene relative Defaults (./token.json,
    GOOGLE_APPLICATION_CREDENTIALS=./credentials.json aus la-maquina-de-
    confianza/.env) gegen das Arbeitsverzeichnis des AUFRUFENDEN Prozesses
    aufgelöst würden (hier: uvicorn in novara-agents/, nicht in
    la-maquina-de-confianza/) — ohne diese Overrides würde entweder die
    Authentifizierung fehlschlagen oder fälschlich der interaktive
    Browser-OAuth-Login ausgelöst (in einem Server-Prozess nicht auslösbar).
    """
    global _crm_handler_module
    if _crm_handler_module is not None:
        return _crm_handler_module

    repo_dir = _resolve_sibling_repo()
    if not repo_dir.is_dir():
        raise RuntimeError(
            f"la-maquina-de-confianza nicht gefunden unter {repo_dir} — "
            "LA_MAQUINA_DE_CONFIANZA_PATH in .env prüfen."
        )

    # setdefault, nicht direktes Setzen: eine explizit im novara-agents-.env
    # vorgegebene Konfiguration soll Vorrang behalten, nicht überschrieben
    # werden.
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(repo_dir / "credentials.json"))
    os.environ.setdefault("NOVARA_CRM_TOKEN_PATH", str(repo_dir / "token.json"))

    from dotenv import load_dotenv
    # override=False: crm_handler.py ruft beim eigenen Import ebenfalls
    # load_dotenv() auf — die hier bereits gesetzten os.environ-Werte
    # (inkl. der beiden obigen Overrides) dürfen dadurch nicht verloren gehen.
    load_dotenv(dotenv_path=repo_dir / ".env", override=False)

    if str(repo_dir) not in sys.path:
        sys.path.insert(0, str(repo_dir))
    import crm_handler  # type: ignore[import-not-found]

    _crm_handler_module = crm_handler
    return crm_handler


def add_lead_to_live_crm(lead_data: dict[str, str]) -> dict[str, Any]:
    """
    Schreibt einen Lead ins echte Produktions-Sheet (crm_handler.add_lead_to_crm).
    Wirft bei fehlender Konfiguration/Auth/Verbindung eine Exception — der
    Aufrufer (CRMIntegrationSDR.upsert_lead) meldet das bewusst als
    success=False zurück, statt den Fehler stillschweigend hinter einem
    Mock-Fallback zu verstecken. Gerade in der Einführungsphase (Block C1)
    soll ein Live-Schreibfehler sichtbar sein, nicht verschluckt werden.
    """
    handler = _load_crm_handler()
    return handler.add_lead_to_crm(lead_data)
