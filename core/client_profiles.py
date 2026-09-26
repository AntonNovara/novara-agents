"""
Betriebsprofile für das Piloten-Rezeptionsprogramm (clients/<client_id>.json).

Gegenstück zu core/knowledge.py (load_wissen): dort lädt Novara SEIN EIGENES
Wissen für den eigenen Vertriebs-Chat, hier lädt agents/client_receptionist_agent.py
das Wissen EINES PILOTKUNDEN, damit derselbe Chat-Widget-Code (static/
chat_widget.js) auf der Website eines Elektrikerbetriebs dessen eigene
Leistungen/Preise/Notdienst-Regeln beantwortet, statt Novaras Pakete zu
bewerben (das wäre für einen Besucher der Kundenwebsite falsch/verwirrend).

Bewusst ein strukturiertes JSON-Schema (Pydantic-validiert), keine freie
Textdatei wie novara_wissen.txt -- die Felder entsprechen 1:1 den Fragen
aus dem Aufnahmegespräch (siehe clients/README.md), damit ein neues
Kundenprofil ohne Prosa-Schreibarbeit direkt aus dem Gesprächsprotokoll
befüllt werden kann.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

_ROOT = Path(__file__).resolve().parent.parent
_CLIENTS_DIR = _ROOT / "clients"


class FAQEntry(BaseModel):
    frage: str
    antwort: str


class ClientProfile(BaseModel):
    """Ein Betriebsprofil -- Pflichtfelder sind genau die, ohne die der
    Rezeptionist-Agent keine sinnvolle Antwort geben könnte (Firmenname,
    Leistungen); alles andere hat einen neutralen Default, damit ein
    Pilot auch mit einem unvollständig ausgefüllten Profil starten kann,
    statt komplett zu blockieren."""

    client_id: str
    firmenname: str
    branche: str = "Handwerksbetrieb"
    servicegebiet: str = ""
    leistungen: list[str] = Field(default_factory=list)
    notdienst_regel: str = ""  # z. B. "Bei Stromausfall/Kurzschluss sofort auf Notdienst hinweisen: 0664 123456"
    buchungslink: str = ""
    haeufige_fragen: list[FAQEntry] = Field(default_factory=list)
    ansprechpartner: str = ""
    telefonnummer: str = ""
    ton: str = "freundlich, professionell, per Sie"
    # Nur Firmenname/Leistungen im Chat nennen, NIE eigene Preise erfinden,
    # solange der Kunde keine expliziten Preisangaben im Profil hinterlegt
    # hat (die meisten Handwerker nennen Preise ohnehin lieber im
    # persönlichen Gespräch/Vor-Ort-Termin als pauschal im Chat).
    preise_oeffentlich: bool = False
    preishinweise: str = ""


class ClientProfileNotFoundError(Exception):
    """Kein Profil unter clients/<client_id>.json gefunden."""


class ClientProfileInvalidError(Exception):
    """Profil gefunden, aber JSON ungültig oder Pflichtfelder fehlen."""


@lru_cache(maxsize=None)
def load_client_profile(client_id: str) -> ClientProfile:
    """
    Lädt und cached ein Betriebsprofil einmalig pro Prozess (gleiches Muster
    wie core.knowledge.load_wissen -- ein Redeploy ist nötig, damit eine
    Profiländerung wirkt, das ist für die Größenordnung von 3 Pilotkunden
    akzeptabel und vermeidet unnötige Datei-I/O auf jedem Chat-Turn).
    """
    safe_id = client_id.strip().lower()
    if not safe_id or "/" in safe_id or ".." in safe_id:
        raise ClientProfileNotFoundError(f"Ungültige client_id: {client_id!r}")

    path = _CLIENTS_DIR / f"{safe_id}.json"
    if not path.is_file():
        raise ClientProfileNotFoundError(
            f"Kein Betriebsprofil für '{client_id}' gefunden unter {path}"
        )
    try:
        return ClientProfile.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise ClientProfileInvalidError(
            f"Betriebsprofil '{client_id}' ist ungültig: {exc}"
        ) from exc


def client_profile_exists(client_id: str) -> bool:
    safe_id = client_id.strip().lower()
    return bool(safe_id) and "/" not in safe_id and (_CLIENTS_DIR / f"{safe_id}.json").is_file()
