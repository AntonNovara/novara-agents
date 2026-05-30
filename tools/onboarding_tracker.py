"""
Onboarding Tracker – personalisierte Checklisten und Kunden-Aktivierungs-Tracking.
In Produktion: httpx-Client gegen internes CS-System oder HubSpot Onboarding-Pipeline.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class ChecklistItem(BaseModel):
    item_id: str
    category: str    # account_setup | integrations | training | legal | communication
    title: str
    description: str
    owner: str       # customer | novara_cs | novara_tech
    due_days: int    # days from contract start
    required: bool
    completed: bool = False


class OnboardingRecord(BaseModel):
    onboarding_id: str = Field(default_factory=lambda: f"ONB-{uuid.uuid4().hex[:8].upper()}")
    company_name: str
    contact_name: str
    contact_email: str
    plan: str                    # starter | pro | enterprise
    industry: str
    team_size: Optional[int]
    primary_use_case: str
    checklist: list[ChecklistItem]
    health_score: int = 0        # starts at 0, increases as items complete
    status: str = "initiated"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    source_agent: str = "onboarding"


class OnboardingResult(BaseModel):
    success: bool
    onboarding_id: str
    message: str
    response: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Checklist templates
# ---------------------------------------------------------------------------
# Format per item: (id, category, title, description, owner, due_days, required)

_BASE: list[tuple] = [
    ("base-01", "account_setup", "Admin-Account aktivieren",
     "Aktivierungslink in der Willkommens-E-Mail öffnen und Passwort setzen.",
     "customer", 1, True),
    ("base-02", "account_setup", "Erstes Team-Mitglied einladen",
     "Einstellungen → Team → Einladen. Mindestens einen Kollegen hinzufügen.",
     "customer", 2, True),
    ("base-03", "communication", "Kick-off-Call vereinbaren",
     "Ihr Customer-Success-Manager meldet sich per E-Mail zur Terminvereinbarung.",
     "novara_cs", 2, True),
    ("base-04", "integrations", "Erste App-Integration verbinden",
     "Integrations-Hub öffnen und die meistgenutzte App verbinden (z.B. CRM, E-Mail).",
     "customer", 3, True),
    ("base-05", "training", "Quickstart-Guide durcharbeiten",
     "docs.novara.io/quickstart – ca. 20 Minuten. Erklärt Kernkonzepte und erste Workflows.",
     "customer", 5, True),
]

_PRO: list[tuple] = [
    ("pro-01", "account_setup", "Custom-Workspace-Domain einrichten",
     "Einstellungen → Domain → eigene Subdomain aktivieren.",
     "customer", 5, False),
    ("pro-02", "integrations", "Zwei Kern-Integrationen aktivieren",
     "CRM- und E-Mail-Plattform verbinden für einen vollständigen Datenfluss.",
     "customer", 7, True),
    ("pro-03", "training", "Solution-Engineering-Call (30 min)",
     "Ihr SE zeigt die wichtigsten Use Cases für Ihre Branche.",
     "novara_cs", 3, True),
    ("pro-04", "integrations", "Ersten vollständigen Workflow aktivieren",
     "Mit dem SE gemeinsam den ersten produktiven Automatisierungs-Workflow bauen.",
     "customer", 7, True),
]

_ENTERPRISE: list[tuple] = [
    ("ent-01", "account_setup", "Dedizierter Customer Success Manager zugewiesen",
     "Ihr persönlicher CSM ist direkter Ansprechpartner für alle Fragen und Eskalationen.",
     "novara_cs", 1, True),
    ("ent-02", "communication", "Dedizierter Slack-Kanal einrichten",
     "Ihr CSM legt einen gemeinsamen Slack-Kanal für schnelle Kommunikation an.",
     "novara_cs", 1, True),
    ("ent-03", "legal", "SLA-Dokument unterzeichnen",
     "Das SLA wird per DocuSign zugestellt – Unterzeichnung bis Tag 3 erforderlich.",
     "customer", 3, True),
    ("ent-04", "account_setup", "SSO / SAML konfigurieren",
     "Einstellungen → Sicherheit → SSO. Ihr IT-Team erhält eine detaillierte Anleitung.",
     "novara_tech", 5, True),
    ("ent-05", "training", "Custom-Training-Session (90 min)",
     "Maßgeschneidertes Team-Training – Termin wird im Kick-off-Call vereinbart.",
     "novara_cs", 10, True),
]

_INDUSTRY: dict[str, list[tuple]] = {
    "healthcare": [
        ("ind-hc-01", "legal", "DSGVO-Datenschutzvereinbarung unterzeichnen",
         "Pflichtdokument für Gesundheitsdaten – wird zusammen mit der Willkommens-E-Mail zugestellt.",
         "customer", 1, True),
    ],
    "financial": [
        ("ind-fs-01", "legal", "Auftragsverarbeitungsvertrag (AVV) unterzeichnen",
         "Pflichtdokument nach DSGVO Art. 28 – wird per DocuSign zugestellt.",
         "customer", 2, True),
    ],
    "manufacturing": [
        ("ind-mf-01", "integrations", "ERP-Integration konfigurieren",
         "SAP / Sage / Navision mit Novara verbinden für automatische Datensynchronisation.",
         "novara_tech", 5, False),
    ],
    "e-commerce": [
        ("ind-ec-01", "integrations", "Shop-System-Integration aktivieren",
         "Shopify / WooCommerce / Magento verbinden für Bestellautomatisierung.",
         "customer", 3, True),
    ],
    "logistics": [
        ("ind-lg-01", "integrations", "Carrier-API-Integration einrichten",
         "Carrier-Schnittstellen anbinden für automatisches Sendungs-Tracking.",
         "novara_tech", 5, False),
    ],
    "real estate": [
        ("ind-re-01", "integrations", "Immobilienverwaltungs-Software anbinden",
         "Haufe iX-Haus / Flowfact o.ä. für Mieter- und Vertrags-Workflows verbinden.",
         "novara_tech", 5, False),
    ],
}


def build_checklist(plan: str, industry: str) -> list[ChecklistItem]:
    """Build a personalized checklist based on plan and industry."""
    plan_l = plan.lower().strip()
    industry_l = industry.lower().strip()

    raw: list[tuple] = list(_BASE)
    if plan_l in ("pro", "enterprise"):
        raw += _PRO
    if plan_l == "enterprise":
        raw += _ENTERPRISE

    for keyword, items in _INDUSTRY.items():
        if keyword in industry_l:
            raw += items
            break  # one industry-specific block is enough

    return [
        ChecklistItem(
            item_id=r[0], category=r[1], title=r[2],
            description=r[3], owner=r[4], due_days=r[5], required=r[6],
        )
        for r in raw
    ]


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class OnboardingTracker:
    """
    Mock-Onboarding-Client.
    In Produktion: httpx.post(endpoint + "/onboardings", json=record.model_dump())
    """

    def __init__(self) -> None:
        self._mock_store: list[dict] = []

    def create_onboarding(self, record: OnboardingRecord) -> OnboardingResult:
        self._mock_store.append(record.model_dump())

        logger.info(
            "Onboarding created",
            extra={
                "id": record.onboarding_id,
                "plan": record.plan,
                "items": len(record.checklist),
            },
        )

        return OnboardingResult(
            success=True,
            onboarding_id=record.onboarding_id,
            message=(
                f"Onboarding für '{record.company_name}' ({record.plan}-Plan) "
                f"gestartet – {len(record.checklist)} Checklisten-Punkte angelegt."
            ),
            response={
                "onboarding_id": record.onboarding_id,
                "plan": record.plan,
                "checklist_items": len(record.checklist),
                "required_items": sum(1 for i in record.checklist if i.required),
                "status": record.status,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

    def get_all_mock_records(self) -> list[dict]:
        return list(self._mock_store)
