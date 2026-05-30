"""
Lead-Datenbank – Mock-Implementierung.
In Produktion: Ersetze durch CRM-API (HubSpot, Salesforce, Pipedrive)
oder LinkedIn Sales Navigator.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProspectContact:
    contact_id: str
    first_name: str
    last_name: str
    title: str
    seniority: str          # c_level | director | manager | ic
    company: str
    company_size: int       # approx. employees
    industry: str
    email: str
    linkedin_url: str
    pain_points: tuple[str, ...]
    tech_stack: tuple[str, ...]


@dataclass
class LeadSearchResult:
    contact: ProspectContact
    match_score: float      # 0.0–1.0
    match_reason: str       # "company_name" | "industry"


# ---------------------------------------------------------------------------
# Prospect corpus (15 mock German B2B companies, Novara target segment)
# ---------------------------------------------------------------------------

_DB: list[ProspectContact] = [
    ProspectContact(
        contact_id="p-001",
        first_name="Lena", last_name="Koch",
        title="Head of Operations", seniority="director",
        company="FastBox Logistics GmbH", company_size=320, industry="Logistics",
        email="l.koch@fastbox-logistics.de",
        linkedin_url="linkedin.com/in/lena-koch-fastbox",
        pain_points=("manuelle Tourenplanung per Excel", "kein Echtzeit-Tracking", "fehlendes TMS"),
        tech_stack=("Excel", "SAP R/3", "WhatsApp"),
    ),
    ProspectContact(
        contact_id="p-002",
        first_name="Lars", last_name="Ehrhardt",
        title="COO", seniority="c_level",
        company="GlobalFreight Hamburg AG", company_size=870, industry="Logistics",
        email="l.ehrhardt@globalfreight-hh.de",
        linkedin_url="linkedin.com/in/lars-ehrhardt-globalfreight",
        pain_points=("manuelle Sendungsverfolgung", "keine API-Integration mit Carriern", "hoher Datenpflegeaufwand"),
        tech_stack=("Oracle TMS", "Excel", "Outlook"),
    ),
    ProspectContact(
        contact_id="p-003",
        first_name="Dr. Kai", last_name="Bergmann",
        title="CTO", seniority="c_level",
        company="PräzisionsWerk AG", company_size=480, industry="Manufacturing",
        email="k.bergmann@praezisionswerk.de",
        linkedin_url="linkedin.com/in/kai-bergmann-praezisionswerk",
        pain_points=("manuelles Beschaffungswesen", "ERP nicht mit Shopfloor verbunden", "Excel-Reporting"),
        tech_stack=("SAP S/4HANA", "Excel", "MS Teams"),
    ),
    ProspectContact(
        contact_id="p-004",
        first_name="Sandra", last_name="Müller",
        title="Head of Digital Transformation", seniority="director",
        company="MetallTech KG", company_size=510, industry="Manufacturing",
        email="s.mueller@metalltech.de",
        linkedin_url="linkedin.com/in/sandra-mueller-metalltech",
        pain_points=("fragmentierte IT-Landschaft", "manuelle Qualitätsprotokolle", "keine Prozessautomatisierung"),
        tech_stack=("Siemens MES", "Excel", "Confluence"),
    ),
    ProspectContact(
        contact_id="p-005",
        first_name="Sophie", last_name="Hartmann",
        title="CEO", seniority="c_level",
        company="ShopNow GmbH", company_size=92, industry="E-Commerce",
        email="s.hartmann@shopnow.de",
        linkedin_url="linkedin.com/in/sophie-hartmann-shopnow",
        pain_points=("manuelle Bestellabwicklung", "kein automatisiertes Retourenmanagement", "Inventar-Sync fehlt"),
        tech_stack=("Shopify", "Excel", "Slack"),
    ),
    ProspectContact(
        contact_id="p-006",
        first_name="Lena", last_name="Becker",
        title="Head of Operations", seniority="director",
        company="Trendhaus Online GmbH", company_size=155, industry="E-Commerce",
        email="l.becker@trendhaus-online.de",
        linkedin_url="linkedin.com/in/lena-becker-trendhaus",
        pain_points=("Bestandsführung per CSV-Export", "manuelle Kundenkommunikation", "kein 3PL-Tracking"),
        tech_stack=("WooCommerce", "DATEV", "Excel"),
    ),
    ProspectContact(
        contact_id="p-007",
        first_name="Dr. Andreas", last_name="Schreiber",
        title="COO", seniority="c_level",
        company="FinanzKlar GmbH", company_size=200, industry="Financial Services",
        email="a.schreiber@finanzklar.de",
        linkedin_url="linkedin.com/in/andreas-schreiber-finanzklar",
        pain_points=("manuelle Reporterstellung", "keine automatisierte KYC-Prüfung", "PDF-Dokumentenprozesse"),
        tech_stack=("Salesforce", "Excel", "DocuSign"),
    ),
    ProspectContact(
        contact_id="p-008",
        first_name="Nicole", last_name="Fischer",
        title="Head of IT", seniority="director",
        company="Versicherung Plus AG", company_size=890, industry="Financial Services",
        email="n.fischer@versicherung-plus.de",
        linkedin_url="linkedin.com/in/nicole-fischer-vplus",
        pain_points=("Legacy-Systeme", "manuelle Schadensbearbeitung", "kein digitaler Antragsprozess"),
        tech_stack=("IBM AS/400", "Excel", "Outlook"),
    ),
    ProspectContact(
        contact_id="p-009",
        first_name="Julia", last_name="Braun",
        title="Head of IT", seniority="director",
        company="MedLogic GmbH", company_size=175, industry="Healthcare",
        email="j.braun@medlogic.de",
        linkedin_url="linkedin.com/in/julia-braun-medlogic",
        pain_points=("manuelle Patientenaufnahme", "keine digitale Terminbestätigung", "fehlendes DMS"),
        tech_stack=("CGM MEDISTAR", "Excel", "Fax"),
    ),
    ProspectContact(
        contact_id="p-010",
        first_name="Stefan", last_name="Ohlmann",
        title="CEO", seniority="c_level",
        company="Autohaus Ohlmann Gruppe", company_size=130, industry="Automotive",
        email="s.ohlmann@autohaus-ohlmann.de",
        linkedin_url="linkedin.com/in/stefan-ohlmann-autohaus",
        pain_points=("Kundendaten in Excel", "manuelle Serviceerinnerungen", "kein digitales DMS"),
        tech_stack=("DealerSocket", "Excel", "Outlook"),
    ),
    ProspectContact(
        contact_id="p-011",
        first_name="Carolin", last_name="Seitz",
        title="Head of Operations", seniority="director",
        company="ImmoFirst GmbH", company_size=195, industry="Real Estate",
        email="c.seitz@immofirst.de",
        linkedin_url="linkedin.com/in/carolin-seitz-immofirst",
        pain_points=("manuelle Mieterverwaltung", "PDF-Vertragsworkflows", "keine automatisierte Nebenkostenabrechnung"),
        tech_stack=("Haufe iX-Haus", "Excel", "DocuSign"),
    ),
    ProspectContact(
        contact_id="p-012",
        first_name="Oliver", last_name="Wendl",
        title="CTO", seniority="c_level",
        company="ConsultWorks GmbH", company_size=140, industry="Professional Services",
        email="o.wendl@consultworks.de",
        linkedin_url="linkedin.com/in/oliver-wendl-consultworks",
        pain_points=("manuelle Zeiterfassung", "kein automatisiertes Reporting", "fragmentierte Projektdokumentation"),
        tech_stack=("JIRA", "Excel", "Confluence"),
    ),
    ProspectContact(
        contact_id="p-013",
        first_name="Patrick", last_name="Neumann",
        title="COO", seniority="c_level",
        company="FoodDist GmbH", company_size=260, industry="Food & Beverage",
        email="p.neumann@fooddist.de",
        linkedin_url="linkedin.com/in/patrick-neumann-fooddist",
        pain_points=("manuelle Bestellannahme", "keine MHD-Automatisierung", "Lieferantenanbindung per E-Mail"),
        tech_stack=("Sage", "Excel", "Outlook"),
    ),
    ProspectContact(
        contact_id="p-014",
        first_name="Maria", last_name="Fröhlich",
        title="Head of Digital", seniority="director",
        company="RetailChain Süd GmbH", company_size=280, industry="Retail",
        email="m.froelich@retailchain-sued.de",
        linkedin_url="linkedin.com/in/maria-froelich-retailchain",
        pain_points=("manuelle Lagerbestandsführung", "kein automatisiertes Reorder", "Filialkommunikation per E-Mail"),
        tech_stack=("Microsoft Navision", "Excel", "Teams"),
    ),
    ProspectContact(
        contact_id="p-015",
        first_name="Tobias", last_name="Grunewald",
        title="CEO", seniority="c_level",
        company="BauProfi KG", company_size=65, industry="Construction",
        email="t.grunewald@bauprofi.de",
        linkedin_url="linkedin.com/in/tobias-grunewald-bauprofi",
        pain_points=("Angebotserstellung per Word", "manuelle Nachtragserfassung", "kein digitales Bautagebuch"),
        tech_stack=("Word", "Excel", "WhatsApp"),
    ),
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class LeadDatabase:
    """
    Fuzzy-Suche in der Prospect-Datenbank nach Firmenname oder Branche.
    In Produktion: CRM-API-Call oder Sales-Navigator-Integration.
    """

    def __init__(self, entries: list[ProspectContact] = _DB) -> None:
        self._entries = entries

    def search(
        self,
        company_name: str,
        industry: str = "",
        top_k: int = 3,
    ) -> list[LeadSearchResult]:
        results: list[LeadSearchResult] = []

        for contact in self._entries:
            name_score = self._name_overlap(company_name, contact.company)
            if name_score >= 0.4:
                results.append(LeadSearchResult(
                    contact=contact,
                    match_score=name_score,
                    match_reason="company_name",
                ))
                continue

            if industry:
                ind_score = self._industry_overlap(industry, contact.industry)
                if ind_score >= 0.5:
                    results.append(LeadSearchResult(
                        contact=contact,
                        match_score=ind_score * 0.6,  # lower weight than name match
                        match_reason="industry",
                    ))

        results.sort(key=lambda r: r.match_score, reverse=True)
        logger.debug(
            "Lead search",
            extra={"company": company_name, "industry": industry, "hits": len(results)},
        )
        return results[:top_k]

    # --- Helpers ---

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {t for t in re.findall(r"[a-zäöüß]{3,}", text.lower()) if t not in _STOP_WORDS}

    def _name_overlap(self, a: str, b: str) -> float:
        ta, tb = self._tokenize(a), self._tokenize(b)
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    def _industry_overlap(self, query: str, db_industry: str) -> float:
        q = query.lower()
        d = db_industry.lower()
        if q == d:
            return 1.0
        # Check if either is a substring of the other
        if q in d or d in q:
            return 0.8
        # Token overlap
        tq, td = self._tokenize(query), self._tokenize(db_industry)
        if not tq or not td:
            return 0.0
        return len(tq & td) / len(tq | td)


_STOP_WORDS: frozenset[str] = frozenset({
    "gmbh", "ag", "kg", "ug", "ltd", "inc", "und", "the", "for", "von", "der", "die", "das",
})
