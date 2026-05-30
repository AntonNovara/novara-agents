"""
FAQ-Datenbank – Mock-Implementierung.
In Produktion: Ersetze _score() durch Cosine-Similarity auf Embeddings
(Weaviate / Qdrant / pgvector).
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
class FAQEntry:
    id: str
    category: str
    question: str
    answer: str
    keywords: tuple[str, ...]


@dataclass
class FAQSearchResult:
    entry: FAQEntry
    score: float            # 0.0–1.0  (fraction of query tokens matched)
    matched_keywords: list[str]


# ---------------------------------------------------------------------------
# FAQ corpus (Novara Automation – German B2B context)
# ---------------------------------------------------------------------------

_FAQ_ENTRIES: list[FAQEntry] = [
    FAQEntry(
        id="faq-001",
        category="onboarding",
        question="Wie starte ich mit Novara Automation?",
        answer=(
            "Der Einstieg in Novara Automation geht in drei Schritten: "
            "1) Registrieren Sie sich unter novara.io/signup und wählen Sie Ihren Plan. "
            "2) Verbinden Sie Ihre erste App im Integrations-Hub. "
            "3) Nutzen Sie eine unserer Workflow-Vorlagen oder starten Sie einen leeren Workflow. "
            "Unsere Schnellstart-Doku finden Sie unter docs.novara.io/quickstart."
        ),
        keywords=(
            "starten", "anfangen", "onboarding", "einrichten", "setup",
            "beginn", "registrieren", "account", "konto", "erste schritte",
            "loslegen", "neu", "starte",
        ),
    ),
    FAQEntry(
        id="faq-002",
        category="integrations",
        question="Welche Integrationen unterstützt Novara?",
        answer=(
            "Novara unterstützt über 200 Integrationen, darunter: "
            "CRM (Salesforce, HubSpot, Pipedrive), "
            "Kommunikation (Slack, Microsoft Teams, Gmail), "
            "ERP & Buchhaltung (SAP, Datev, Lexoffice) "
            "sowie offene Webhooks und eine REST-API für individuelle Anbindungen. "
            "Die vollständige Liste: docs.novara.io/integrations."
        ),
        keywords=(
            "integration", "verbinden", "anbinden", "app", "zapier", "make",
            "webhook", "salesforce", "hubspot", "slack", "teams", "erp",
            "verbindung", "connector", "plugin",
        ),
    ),
    FAQEntry(
        id="faq-003",
        category="billing",
        question="Wie funktioniert die Abrechnung?",
        answer=(
            "Novara berechnet monatlich oder jährlich (20 % Rabatt bei Jahresplan). "
            "Alle Pläne enthalten eine feste Anzahl an Workflow-Ausführungen pro Monat; "
            "bei Überschreitung fallen Pay-as-you-go-Kosten an. "
            "Rechnungen werden automatisch per E-Mail zugestellt. "
            "Zahlungsarten: Kreditkarte, SEPA-Lastschrift, auf Rechnung ab Enterprise-Plan. "
            "Details: novara.io/pricing."
        ),
        keywords=(
            "rechnung", "abrechnung", "preis", "kosten", "billing", "zahlung",
            "plan", "abo", "subscription", "tarif", "enterprise", "sepa",
            "kreditkarte", "bezahlen", "faktura", "invoice",
        ),
    ),
    FAQEntry(
        id="faq-004",
        category="technical",
        question="Wie richte ich eine API-Verbindung ein?",
        answer=(
            "1) Navigieren Sie zu Einstellungen → API-Schlüssel und erstellen Sie einen neuen Key. "
            "2) Kopieren Sie den Key – er wird nur einmalig angezeigt. "
            "3) Übergeben Sie ihn im Header: 'Authorization: Bearer <key>'. "
            "Base-URL: api.novara.io/v1. "
            "Vollständige API-Referenz: docs.novara.io/api."
        ),
        keywords=(
            "api", "key", "token", "authentifizierung", "endpoint", "auth",
            "bearer", "header", "rest", "request", "http", "schnittstelle",
            "zugriffsschlüssel", "credentials",
        ),
    ),
    FAQEntry(
        id="faq-005",
        category="privacy",
        question="Wie verarbeitet Novara meine Daten (DSGVO)?",
        answer=(
            "Novara ist vollständig DSGVO-konform. Alle Daten werden ausschließlich "
            "in EU-Rechenzentren (Frankfurt, Amsterdam) verarbeitet. "
            "Wir schließen mit jedem Kunden einen AV-Vertrag (Art. 28 DSGVO) ab. "
            "Daten werden nach Vertragsende innerhalb von 30 Tagen gelöscht. "
            "Unser Datenschutzbeauftragter: privacy@novara.io."
        ),
        keywords=(
            "dsgvo", "datenschutz", "daten", "gdpr", "privacy", "speicherung",
            "eu", "avv", "auftragsverarbeitung", "compliance", "löschung",
            "sicherheit", "vertraulich", "rechenzentrum",
        ),
    ),
    FAQEntry(
        id="faq-006",
        category="support_hours",
        question="Zu welchen Zeiten ist der Support erreichbar?",
        answer=(
            "Support-Zeiten je nach Plan – "
            "Starter: Mo–Fr 9–17 Uhr (E-Mail, Reaktionszeit 24 h). "
            "Pro: Mo–Fr 8–20 Uhr + Sa 10–16 Uhr (Chat + E-Mail, 4 h). "
            "Enterprise: 24/7 (dedizierter Account Manager, 1 h). "
            "Tickets: support.novara.io. Notfall-Hotline (Enterprise): +49 800 NOVARA1."
        ),
        keywords=(
            "support", "hilfe", "kontakt", "erreichbar", "öffnungszeiten",
            "ticket", "anfrage", "email", "chat", "sla", "reaktionszeit",
            "telefonieren", "hotline", "wann",
        ),
    ),
    FAQEntry(
        id="faq-007",
        category="technical",
        question="Mein Workflow läuft nicht – was kann ich tun?",
        answer=(
            "Prüfen Sie zunächst das Ausführungs-Log unter Workflows → Verlauf. "
            "Häufige Ursachen: abgelaufene OAuth-Tokens (Verbindung neu autorisieren), "
            "fehlerhafte Feldmapping-Konfiguration oder API-Limits der Ziel-App. "
            "Quick-Fix-Guide: docs.novara.io/troubleshooting. "
            "Hält das Problem an, erstellen Sie ein Support-Ticket mit dem Log-Export."
        ),
        keywords=(
            "fehler", "error", "workflow", "problem", "bug", "funktioniert",
            "läuft", "broken", "ausführung", "log", "trigger", "scheitert",
            "crash", "defekt", "kaputt", "geht nicht",
        ),
    ),
    FAQEntry(
        id="faq-008",
        category="onboarding",
        question="Gibt es eine kostenlose Testphase oder Demo?",
        answer=(
            "Ja – jeder neue Account erhält automatisch 14 Tage Pro-Trial (keine Kreditkarte nötig). "
            "Zusätzlich bieten wir kostenlose 30-Minuten-Demos mit einem Solution Engineer an. "
            "Demo buchen: novara.io/demo. Trial starten: novara.io/signup."
        ),
        keywords=(
            "demo", "test", "trial", "kostenlos", "probieren", "free",
            "testphase", "ausprobieren", "gratis", "tage", "pilot", "testen",
        ),
    ),
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class FAQDatabase:
    """
    Keyword-basierte FAQ-Suche mit 5-Zeichen-Präfix-Stemming.
    Ersatz für Embedding-Suche in der lokalen Entwicklungsumgebung.
    """

    FAQ_ESCALATION_THRESHOLD = 0.30  # below this → escalate to ticket

    def __init__(self, entries: list[FAQEntry] = _FAQ_ENTRIES) -> None:
        self._entries = entries

    def search(self, query: str, top_k: int = 3) -> list[FAQSearchResult]:
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scored: list[FAQSearchResult] = []
        for entry in self._entries:
            kw_tokens = list(entry.keywords) + self._tokenize(entry.question)
            matched = [qt for qt in query_tokens if any(self._match(qt, kw) for kw in kw_tokens)]

            if matched:
                score = len(set(matched)) / len(set(query_tokens))
                scored.append(FAQSearchResult(
                    entry=entry,
                    score=round(score, 4),
                    matched_keywords=sorted(set(matched)),
                ))

        scored.sort(key=lambda r: r.score, reverse=True)
        results = scored[:top_k]
        logger.debug(
            "FAQ search completed",
            extra={"tokens": query_tokens, "hits": len(results)},
        )
        return results

    # --- Helpers ---

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"[a-zäöüß]{3,}", text.lower())

    @staticmethod
    def _stem(word: str) -> str:
        return word[:5]

    def _match(self, a: str, b: str) -> bool:
        if a == b:
            return True
        if len(a) >= 4 and len(b) >= 4:
            return self._stem(a) == self._stem(b)
        return False
