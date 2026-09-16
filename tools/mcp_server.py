"""
MCP Server – exponiert Novara-Tools (LeadDatabase, CRMIntegrationSDR,
DealTracker) über das native Model Context Protocol (MCP) für externe
Kunden-CRMs (HubSpot, Salesforce, Pipedrive, ...).

Warum ein eigener MCP-Server statt der bestehenden FastAPI-Gateway-Routen
(main.py)? Die REST-API in main.py ist für Novaras eigene 5
LangGraph-Agenten gebaut (Request/Response-Schema AgentRequest/
AgentResponse, X-API-Key-Auth, DLP-Wrapper über BaseAgent). Ein Kunden-CRM
will dagegen nicht "mit einem Novara-Agenten sprechen", sondern einzelne,
klar typisierte Werkzeuge direkt aufrufen (Lead suchen, Lead anlegen, Deal
aktualisieren) -- genau das Muster, für das MCP entworfen wurde:
strukturierte Tool-Discovery + strukturierte Ein-/Ausgaben, standardisiert
über jeden MCP-fähigen Client hinweg (nicht nur Claude).

Scope: 3 Tools, 1:1 auf bestehende Tool-Klassen abgebildet -- KEINE eigene
Geschäftslogik hier, nur Typprüfung + Delegation:
  - search_leads   -> tools.lead_database.LeadDatabase.search()
  - upsert_lead    -> tools.crm_integration.CRMIntegrationSDR.upsert_lead()
  - upsert_deal    -> tools.deal_tracker.DealTracker.upsert_deal()

Beide Ziel-Tools (CRMIntegrationSDR, DealTracker) bleiben In-Memory-Mocks
(siehe deren eigene Docstrings) -- dieser Server ändert nichts an deren
Backend, er macht sie nur zusätzlich über MCP erreichbar, zusätzlich zu den
5 Agenten-Graphen. Läuft als eigener Prozess-Singleton mit eigenem
In-Memory-Store (_leads/_crm/_deals unten) -- geteilt NUR zwischen MCP-
Tool-Aufrufen innerhalb dieses Prozesses, nicht mit main.py's Agenten-
Prozess. Schreibt ein Kunden-CRM hier einen Lead, den ein SDR-Agent-Lauf
später erneut anlegt, entstehen zwei getrennte CRM-Einträge -- dieselbe
Einschränkung wie beim Fehlen einer echten CRM-Primärschlüssel-Kopplung
insgesamt (siehe core/customer_state.py, "Bekannte Einschränkungen" in
CLAUDE.md).

Transport: stdio (Default -- für lokale/Desktop-MCP-Clients wie Claude
Desktop) oder optional HTTP (streamable-http, für entfernte Kunden-CRMs).

TODO vor Produktivbetrieb mit einem entfernten Kunden-CRM: Der
HTTP-Transport hat hier KEINE eigene Auth -- anders als main.py's
X-API-Key-Header ist das kein MCP-Primitive, das FastMCP von sich aus
mitbringt. Muss vor dem ersten Kunden-CRM-Zugriff hinter denselben Schutz
wie main.py (Reverse-Proxy mit eigenem Auth, oder FastMCP's
auth_server_provider für OAuth) gestellt werden. Für lokale/stdio-Nutzung
(Claude Desktop) ist das kein Thema -- der Prozess läuft dann im
Vertrauensbereich des aufrufenden Clients selbst.

Starten:
  python3 -m tools.mcp_server            # stdio (Default)
  python3 -m tools.mcp_server --http      # HTTP auf Port 8001 (streamable-http)

HINWEIS: Anders als der Rest des Repos NUTZT diese Datei bewusst KEIN
`from __future__ import annotations`. FastMCPs Tool-Registrierung
(`Tool.from_function()`) löst Parameter-Annotationen zur Registrierungszeit
per `issubclass()` auf -- mit postponed evaluation (PEP 563) sind
Annotationen dann Strings statt echter Typen, was beim Import mit einem
TypeError crasht (getestet gegen mcp==1.12.4). Alle Type-Hints hier sind
daher bewusst PEP-585-kompatibel (`list[...]`, `dict[...]`) statt der
neueren `X | None`-Syntax, damit die Datei ohne den Future-Import auch unter
Python 3.9/3.10 (falls je nötig) syntaktisch gültig bleibt.
"""
import argparse
import logging
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from tools.crm_integration import CRMIntegrationSDR, LeadRecord
from tools.deal_tracker import DealRecord, DealStage, DealTracker
from tools.lead_database import LeadDatabase

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "novara-tools",
    instructions=(
        "Werkzeuge der Novara Agent Factory für Lead-Suche sowie Lead- und "
        "Deal-Pflege. Für Kunden-CRM-Integrationen (HubSpot, Salesforce, "
        "Pipedrive) gedacht, die Novaras interne Prospect-Datenbank und "
        "CRM-/Deal-Pipeline direkt ansprechen wollen, ohne den vollen "
        "SDR- oder Sales-Copilot-Agenten-Workflow zu durchlaufen."
    ),
)

# Prozessweite Instanzen -- gleiches In-Memory-Mock-Muster wie main.py's
# Agent-Registry, nur für diesen MCP-Server-Prozess.
_leads = LeadDatabase()
_crm = CRMIntegrationSDR()
_deals = DealTracker()


@mcp.tool()
def search_leads(company_name: str, industry: str = "", top_k: int = 3) -> list[dict[str, Any]]:
    """
    Sucht Firmenkontakte in Novaras Prospect-Datenbank per Fuzzy-Match auf
    Firmenname oder Branche.

    Args:
        company_name: Firmenname, nach dem gesucht wird (Token-Overlap-Match).
        industry: Optionale Branche als schwächerer Fallback, falls der
            Firmenname keinen Treffer liefert.
        top_k: Maximale Anzahl Treffer (Default 3).

    Returns:
        Liste von Treffern, sortiert nach match_score absteigend, je
        {contact: {...}, match_score, match_reason}.
    """
    results = _leads.search(company_name, industry=industry, top_k=top_k)
    return [
        {
            "contact": {
                "contact_id": r.contact.contact_id,
                "first_name": r.contact.first_name,
                "last_name": r.contact.last_name,
                "title": r.contact.title,
                "seniority": r.contact.seniority,
                "company": r.contact.company,
                "company_size": r.contact.company_size,
                "industry": r.contact.industry,
                "email": r.contact.email,
                "linkedin_url": r.contact.linkedin_url,
                "pain_points": list(r.contact.pain_points),
                "tech_stack": list(r.contact.tech_stack),
            },
            "match_score": r.match_score,
            "match_reason": r.match_reason,
        }
        for r in results
    ]


@mcp.tool()
def upsert_lead(
    company_name: str,
    contact_name: str,
    contact_title: str,
    industry: str,
    lead_score: int,
    icp_tier: str,
    outreach_channel: str,
    outreach_text: str,
    contact_email: Optional[str] = None,
    contact_linkedin: Optional[str] = None,
    company_size: Optional[int] = None,
    outreach_subject: Optional[str] = None,
    pain_points: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Legt einen Lead in Novaras CRM-Pipeline (outbound-sdr) an oder
    aktualisiert ihn -- dasselbe Backend, das der SDR-Agent nach
    compose_outreach() aufruft (tools/crm_integration.py).

    Für Kunden-CRMs gedacht, die einen extern (z. B. in HubSpot/Salesforce/
    Pipedrive) bereits qualifizierten Lead nach Novara spiegeln wollen, ohne
    den vollen SDR-Graphen (Scoring, Outreach-Generierung, Consent-Check)
    zu durchlaufen -- Aufrufer garantiert selbst, dass Score/Tier/Outreach
    bereits feststehen.

    Args:
        icp_tier: "high" | "medium" | "low"
        outreach_channel: "email" | "linkedin"

    Returns:
        {success, lead_id, message, crm_response}
    """
    record = LeadRecord(
        company_name=company_name,
        contact_name=contact_name,
        contact_title=contact_title,
        contact_email=contact_email,
        contact_linkedin=contact_linkedin,
        industry=industry,
        company_size=company_size,
        lead_score=lead_score,
        icp_tier=icp_tier,
        outreach_channel=outreach_channel,
        outreach_subject=outreach_subject,
        outreach_text=outreach_text,
        pain_points=pain_points or [],
        contact_source="external_crm",
        source_agent="mcp_server",
    )
    result = _crm.upsert_lead(record)
    return result.model_dump()


@mcp.tool()
def upsert_deal(
    company_name: str,
    contact_name: str,
    contact_title: str,
    deal_stage: str,
    deal_health_score: int,
    close_probability: int,
    meeting_summary: str,
    followup_subject: str,
    followup_body: str,
    objections: Optional[list[dict[str, Any]]] = None,
    buying_signals: Optional[list[str]] = None,
    next_steps: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Legt einen Deal-Datensatz in Novaras Deal-Tracker an oder aktualisiert
    ihn -- dasselbe Backend, das der Sales Copilot Agent nach
    detect_signals()/compose_followup() aufruft (tools/deal_tracker.py).

    Args:
        deal_stage: einer von discovery | demo | proposal | negotiation | closing
        objections: Liste von {text, category, severity}

    Returns:
        {success, deal_id, message, crm_response}

    Raises:
        ValueError: wenn deal_stage keiner der gültigen Werte ist.
    """
    stage_map = {s.value: s for s in DealStage}
    stage = stage_map.get(deal_stage)
    if stage is None:
        valid = ", ".join(stage_map)
        raise ValueError(f"Unbekannte deal_stage '{deal_stage}'. Gültig: {valid}")

    record = DealRecord(
        company_name=company_name,
        contact_name=contact_name,
        contact_title=contact_title,
        deal_stage=stage,
        deal_health_score=deal_health_score,
        close_probability=close_probability,
        objections=objections or [],
        buying_signals=buying_signals or [],
        next_steps=next_steps or [],
        meeting_summary=meeting_summary,
        followup_subject=followup_subject,
        followup_body=followup_body,
        source_agent="mcp_server",
    )
    result = _deals.upsert_deal(record)
    return result.model_dump()


def main() -> None:
    parser = argparse.ArgumentParser(description="Novara MCP-Tool-Server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Über HTTP (streamable-http) statt stdio laufen lassen -- für entfernte Kunden-CRMs.",
    )
    parser.add_argument("--port", type=int, default=8001, help="Port für --http (Default 8001).")
    args = parser.parse_args()

    if args.http:
        mcp.settings.port = args.port
        logger.info("MCP-Server startet über HTTP (streamable-http) auf Port %d", args.port)
        mcp.run(transport="streamable-http")
    else:
        logger.info("MCP-Server startet über stdio")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
