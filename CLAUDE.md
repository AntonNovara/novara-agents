# Novara Agent Factory

Modulares Multi-Agenten-System für B2B-Prozessautomatisierung.  
Jeder Agent kapselt einen eigenständigen Geschäftsprozess als LangGraph-Workflow
und ist über ein gemeinsames FastAPI-Gateway erreichbar.

---

## Schnellstart (lokale Entwicklung)

```bash
# 1. Abhängigkeiten installieren
pip install -r requirements.txt

# 2. Umgebungsvariablen setzen
cp .env.example .env
# → ANTHROPIC_API_KEY in .env eintragen

# 3. Server starten (hot-reload aktiv in development)
python main.py
# oder direkt:
uvicorn main:app --reload --port 8000

# 4. Health-Check
curl http://localhost:8000/health
# → {"status":"healthy","environment":"development","agents":["onboarding","operations","sales-copilot","sdr","support"]}

# Swagger-UI (nur development)
open http://localhost:8000/docs
```

> **Hinweis:** `ENVIRONMENT=development` in `.env` deaktiviert die API-Key-Pflicht
> (`API_SECRET_KEY=dev-secret` gilt als Bypass). Für alle anderen Umgebungen muss
> `X-API-Key: <secret>` im Header mitgegeben werden.

---

## Architektur

```
┌──────────────────────────────────────────────────────────────┐
│                      FastAPI Gateway                          │
│            POST /api/v1/agents/{type}/process                │
│            POST /api/v1/agents/operations/process-file       │
├─────────┬────────────┬──────────────┬──────────┬────────────┤
│ Onboard-│ Operations │  Support     │  SDR     │  Sales     │
│ ing     │  Agent     │  Agent       │  Agent   │  Copilot   │
│ Agent   │ (LangGraph)│ (LangGraph)  │(LangGraph│ (LangGraph)│
├─────────┴────────────┴──────────────┴──────────┴────────────┤
│                 BaseAgent (Security-Wrapper)                  │
│      Input-DLP → _run() → Output-DLP → AgentResponse         │
├──────────────────────────────────────────────────────────────┤
│                          Tools                               │
│  DocumentParser │ FAQDatabase   │ LeadDatabase               │
│  CRMIntegration │ TicketSystem  │ CRMIntegrationSDR          │
│  DealTracker    │ NotificationSystem │ OnboardingTracker      │
├──────────────────────────────────────────────────────────────┤
│                  LLM: Claude (Anthropic)                      │
│          Model: claude-sonnet-4-6  (via                      │
│          langchain-anthropic, temperature=0)                  │
└──────────────────────────────────────────────────────────────┘
```

### Kernprinzipien

| Prinzip | Umsetzung |
|---|---|
| **Security-first** | Jede Request durchläuft DLP/PII-Redaktion (DSGVO Art. 25) vor und nach dem Agenten |
| **LLM-sparend** | Regex/Keyword-Heuristiken zuerst, LLM nur als Fallback oder für Generierungsaufgaben |
| **Einheitliches Interface** | Alle Agenten implementieren `BaseAgent._run()` → gleiche Request/Response-Modelle |
| **Swap-in bereit** | Mock-Implementierungen (CRM, FAQ, Tickets, Leads) haben identische Interfaces zu ihren Prod-Pendants |

---

## Technologie-Stack

| Komponente | Technologie | Version |
|---|---|---|
| API-Framework | FastAPI + uvicorn | 0.115 / 0.32 |
| Agent-Orchestrierung | LangGraph StateGraph | 0.2.x |
| LLM-Client | langchain-anthropic | 0.3.x |
| LLM-Modell | Claude Sonnet (`claude-sonnet-4-6`) | — |
| Datenvalidierung | Pydantic v2 | 2.10 |
| PDF-Extraktion | pdfplumber | 0.11 |
| File-Upload | python-multipart | 0.0.20 |
| Logging | structlog (JSON) | 24.4 |
| Konfiguration | pydantic-settings (.env) | 2.6 |

---

## Request / Response

Alle Agenten teilen dasselbe Schema:

```bash
# Text-Endpoint
POST /api/v1/agents/{agent_type}/process
X-API-Key: <secret>
Content-Type: application/json

{"text": "...", "session_id": "optional-uuid", "metadata": {}}
```

```bash
# File-Upload (nur Operations Agent)
POST /api/v1/agents/operations/process-file
X-API-Key: <secret>
Content-Type: multipart/form-data

file=@rechnung.pdf  session_id=optional-uuid
```

**Response** (`AgentResponse`):

```json
{
  "success": true,
  "session_id": "...",
  "agent_type": "operations",
  "result": { ... },
  "dlp_findings": ["iban: 1 occurrence(s) redacted"],
  "processing_time_ms": 312.4,
  "error": null
}
```

---

## Security Layer (`core/security.py`)

Läuft automatisch um jede `BaseAgent.process()`-Ausführung (Input UND Output,
über `sanitize_dict()`):

| Typ | Pattern | Kategorie | Verhalten |
|---|---|---|---|
| E-Mail | RFC-5322 | Kontakt | erkannt, NICHT redigiert |
| Telefon DE | `+49` / `0…`, endet immer auf einer Ziffer | Kontakt | erkannt, NICHT redigiert |
| Telefon AT | `+43…`, endet immer auf einer Ziffer | Kontakt | erkannt, NICHT redigiert |
| IBAN | ISO 13616 inkl. Leerzeichen | sensibel | `[REDACTED:IBAN]` |
| IP-Adresse | IPv4 | sensibel | `[REDACTED:IP_ADDRESS]` |
| Steuernummer | 2-3/3/4-5 Ziffern, Trennzeichen Leerzeichen/`/`/`-` (Pflicht) | sensibel | `[REDACTED:TAX_ID]` |

**Kontakt vs. sensibel:** E-Mail/Telefon sind Daten, die Agenten für ihre
Aufgabe brauchen (CRM-Eintrag, Welcome-Mail, Outreach, …) und werden daher nur
erkannt (Findings/Audit-Trail), nicht redigiert. Alles andere wird immer
redigiert. Sensible-Daten-Treffer haben bei Überlappung IMMER Vorrang vor
Kontakt-Kandidaten (z. B. eine Steuernummer, deren Ziffern zufällig auch
phone_de's Zeichenklasse erfüllen, bleibt eine Steuernummer und wird
redigiert). Intern per Mask-then-Restore umgesetzt: jeder Kontakt-Treffer
bekommt ein eindeutiges, indexiertes Token (kein gemeinsames Füllzeichen),
damit sich zwei Treffer bei der Wiederherstellung nie vertauschen können,
auch wenn sich ihre Muster überlappen. Regressionstests: `test_system.py`
TEST 9 (u. a. alle bekannten Steuernummer-Formate im System, mehrere
überlappende/benachbarte Kontakte im selben Text, 4 gemischte sensible Werte
gleichzeitig).

> **Offener Punkt: Audit-Trail unterzählt bei zwei Telefonnummern, die nur
> durch ein Leerzeichen getrennt sind.** Verifiziert per `/code-review
> ultra` (3. Durchlauf auf `ca3c6e3`). `phone_de`s Muster
> (`(?:[\s\-]?\d){5,14}`) ist gierig genug, um über das trennende Leerzeichen
> hinweg in eine direkt anschließende zweite Nummer hineinzumatchen — die
> beiden Nummern werden dann als EIN Treffer gezählt statt als zwei. Der
> ausgegebene Text ändert sich dadurch NICHT (Kontaktdaten werden ohnehin nie
> redigiert, nur gezählt), betroffen ist ausschließlich die Findings/
> Audit-Trail-Zahl in `dlp_findings`. Keine Sicherheitslücke, aber ein
> ungenauer Audit-Trail. Bewusst nicht in derselben Runde gefixt — braucht
> eigene Runde.

Hard-Block (Request wird abgelehnt, nicht nur redigiert) bei:
- **Credentials**: Keyword (`password`, `api_key`, `bearer`, …) + Delimiter
  (`:`, `=`, "ist", "is") + Wert — die bloße Erwähnung des Wortes in
  normalem Fließtext blockt nicht mehr.
- **Prompt-Injection**: einfache Keyword-Heuristik (`_INJECTION_MARKERS`),
  portiert aus `sdr_demo_referencia/app/dlp.py`.

> **Offener Punkt: Prompt-Injection-Marker sind zu breit.** Verifiziert per
> `/code-review ultra`: Marker wie `"you are now"`, `"act as"`,
> `"system prompt"` matchen auch echte, harmlose Business-Anfragen und werden
> dadurch tatsächlich abgelehnt (nicht nur geloggt) — anders als der
> Credential-Hard-Block ist dieser Pfad nicht nur ein Logging-Ärgernis.
> Konkrete Repro-Beispiele aus dem Review:
> - `"You are now our primary contact for billing questions going forward."`
> - `"...act as the account owner when configuring SSO."`
> Bewusst NICHT in derselben Runde wie der Credential-Hard-Block-Fix und der
> Kontakt/Sensibel-Umbau angefasst — braucht eine eigene Runde mit Fokus
> ausschließlich darauf, nicht "nebenbei mitgefixt".

---

## LLM-Factory & Demo-Modus (`core/llm.py`)

Alle 5 Text-Agenten (nicht `voice_agent.py`, der die Anthropic-SDK direkt für
Streaming nutzt) beziehen ihren LLM-Client über die zentrale Factory
`core.llm.build_llm(max_tokens=...)` statt über eine pro-Agent duplizierte
`_build_llm()`-Funktion.

**Demo-Modus** (`settings.effective_demo_mode`) ersetzt den echten
`ChatAnthropic`-Call durch einen Fake-Client (`_DemoChatModel`), der anhand des
System-Prompts erkennt, ob JSON oder Freitext (`SUBJECT:`-Format) erwartet wird,
und einen generischen Platzhalter zurückgibt — kein Netzwerk-Call, keine Kosten.

Aktiv, wenn:
- kein echter `ANTHROPIC_API_KEY` gesetzt ist (Default `mock-key`), **oder**
- `DEMO_MODE=true` explizit gesetzt ist — auch mit echtem Key, z. B. um beim
  lokalen Entwickeln keine echten Calls zu verbrauchen.

> **Offener Punkt: `test_system.py`s `live`-Gating prüft nicht
> `settings.effective_demo_mode`.** Verifiziert per `/code-review ultra`
> (3. Durchlauf auf `ca3c6e3`). Die Live-Test-Weiche (`live =
> settings.anthropic_key_configured`, Zeile ~684) fragt nur ab, ob ein
> echter Key gesetzt ist — nicht, ob `DEMO_MODE=true` zusätzlich aktiv ist.
> Ist beides der Fall (echter Key + `DEMO_MODE=true`), laufen die
> "Live"-Tests tatsächlich gegen `_DemoChatModel` statt gegen Claude, werden
> in der Testausgabe aber als echte LLM-Calls behandelt/beschriftet. Kein
> Sicherheitsproblem, nur eine irreführende Testbeschriftung/-abdeckung.
> Bewusst nicht in derselben Runde gefixt — braucht eigene Runde.

> **Nur Entwicklungs-/Kosten-Bequemlichkeit, kein Sicherheits-Mechanismus.**
> Sobald ein Agent öffentlich als Demo exponiert wird (wie
> `sdr_demo_referencia/`), muss dort Demo-Modus zum Fail-Safe-Default werden
> (an, sofern nicht explizit für Prod freigeschaltet) — das ist noch offen.

---

## Implementierte Agenten

### 1. Operations Agent (`agents/operations_agent.py`)

Verarbeitet eingehende Rechnungen (Text oder PDF) und schreibt sie ins ERP/CRM.

**Workflow:**
```
classify_document → extract_fields → validate_extraction → write_to_crm → finalize
                                                        ↓ (Fehler)
                                              finalize_validation_failed
```

**Nodes:**

| Node | Art | Beschreibung |
|---|---|---|
| `classify_document` | Heuristik + LLM-Fallback | Erkennt Rechnungen via Keyword-Zählung (≥2 Hits = fast-path) |
| `extract_fields` | Regex + LLM-Enrichment | Extrahiert Firma, Betrag, Datum, Rechnungsnr. |
| `validate_extraction` | deterministisch | Pflichtfelder: `company_name`, `amount > 0`, `invoice_date` |
| `write_to_crm` | `CRMIntegration` | Mock → in Prod: httpx POST an ERP |
| `finalize` | — | Baut strukturierten Output |

**Tools:**
- `DocumentParser` — Regex-Extraktion + `pdfplumber`-PDF-Support
- `CRMIntegration` / `ERPRecord` — Mock-ERP-Client

**Endpunkte:**
```bash
# Text-Rechnung
POST /api/v1/agents/operations/process

# PDF-Upload
POST /api/v1/agents/operations/process-file \
  -F "file=@rechnung.pdf;type=application/pdf"
```

---

### 2. Support Agent (`agents/support_agent.py`)

Analysiert Kundenanfragen, antwortet aus der FAQ-Datenbank oder eskaliert per Ticket.

**Workflow:**
```
analyze_inquiry → search_faq → [confidence ≥ 0.30?]
                                    ├── ja  → compose_faq_response → finalize
                                    └── nein (oder Beschwerde+high) → create_ticket → finalize
```

**Nodes:**

| Node | Art | Beschreibung |
|---|---|---|
| `analyze_inquiry` | LLM | Klassifiziert Intent / Urgency / Sentiment / Language |
| `search_faq` | `FAQDatabase` | 5-Zeichen-Prefix-Stemming, Schwellwert 0.30 |
| `compose_faq_response` | LLM | Personalisierte Antwort in erkannter Sprache (de/en) |
| `create_ticket` | `TicketSystem` | Priority-Mapping: CRITICAL bei Beschwerde + negativ |
| `finalize` | — | Baut strukturierten Output |

**Eskalationslogik:**
- FAQ-Konfidenz < 0.30 → Ticket
- Intent = `complaint` **und** Urgency = `high` → immer Ticket (forced escalate), unabhängig von FAQ-Treffer

**Ticket-Prioritäten:**

| Bedingung | Priorität |
|---|---|
| Intent `complaint` oder (Urgency `high` + Sentiment `negative`) | `CRITICAL` |
| Urgency `high` | `HIGH` |
| Urgency `medium` | `MEDIUM` |
| Urgency `low` | `LOW` |

**Tools:**
- `FAQDatabase` — 8 FAQ-Einträge (Onboarding, Billing, Technical, Privacy, Support Hours, Integrations)
- `TicketSystem` / `TicketRecord` — Mock → in Prod: Zendesk / Freshdesk / Jira SD

```bash
POST /api/v1/agents/support/process
```

---

### 3. SDR Agent (`agents/sdr_agent.py`)

Qualifiziert eingehende Firmen-Leads, ermittelt Ansprechpartner und erstellt personalisierten Cold-Outreach.

**Workflow:**
```
analyze_input → search_leads → score_lead → [score ≥ 40?]
                                               ├── ja  → compose_outreach → write_to_crm → finalize
                                               └── nein → finalize_disqualified
```

**Nodes:**

| Node | Art | Beschreibung |
|---|---|---|
| `analyze_input` | LLM | Extrahiert Firma, Branche, Größe, Pain Points, ICP-Score (0-100) |
| `search_leads` | `LeadDatabase` | Fuzzy-Match auf Firmenname; Industry-Match → Persona-Generierung |
| `score_lead` | deterministisch | `lead_score = min(100, icp_score + seniority_bonus)` |
| `compose_outreach` | LLM | Hochpersonalisierter E-Mail- oder LinkedIn-Text mit SUBJECT-Parsing |
| `write_to_crm` | `CRMIntegrationSDR` | Mock → in Prod: HubSpot / Salesforce / Pipedrive |
| `finalize_disqualified` | — | Kein CRM-Eintrag, kein Outreach |

**Lead-Scoring:**

| Faktor | Gewicht |
|---|---|
| ICP-Score (LLM-bewertet, 0-100) | Basis |
| Seniority-Bonus: C-Level | +15 |
| Seniority-Bonus: Director / Head of | +10 |
| Seniority-Bonus: Manager | +5 |
| **Disqualifizierungsschwelle** | **< 40** |

**Kontakt-Quellen:**
1. `LeadDatabase` (15 Mock-Firmenkontakte, 10 Branchen) → bei Firmenname-Treffer
2. LLM-generierte Ziel-Persona → bei unbekannter Firma (Name, Titel, Seniority, E-Mail, LinkedIn werden inferiert)

**Tools:**
- `LeadDatabase` / `ProspectContact` — Mock → in Prod: CRM-API oder LinkedIn Sales Navigator
- `CRMIntegrationSDR` / `LeadRecord` — Pipeline `outbound-sdr`, Stage `new_lead`

```bash
POST /api/v1/agents/sdr/process
```

---

### 4. Sales Copilot Agent (`agents/sales_copilot_agent.py`)

Analysiert Verkaufsgespräch-Notizen oder Transkripte, identifiziert Einwände und Next Steps, generiert Follow-up-E-Mail und aktualisiert den Deal im CRM.

**Workflow:**
```
parse_transcript → detect_signals → compose_followup → update_deal → finalize
```

**Nodes:**

| Node | Art | Beschreibung |
|---|---|---|
| `parse_transcript` | LLM | Extrahiert company_name, contact, meeting_date, deal_stage, summary, language |
| `detect_signals` | LLM | Objections [{text, category, severity}], buying_signals, next_steps, deal_health_score, close_probability |
| `compose_followup` | LLM | Follow-up-E-Mail mit SUBJECT:-Zeile, adressiert Einwände konstruktiv |
| `update_deal` | `DealTracker` | Mock → in Prod: HubSpot Deals API / Salesforce Opportunity |
| `finalize` | — | Baut strukturierten Output |

**Objection-Kategorien:** `pricing` | `timing` | `competitor` | `authority` | `need` | `trust` | `complexity`

**Deal-Health-Score-Logik:** Start 50 · +10 pro starkes Buying Signal (max +30) · -10/-5 pro high/medium Objection · +15 bei konkreten Next Steps · Stage-Bonus: +5 bis +20

**Tools:**
- `DealTracker` / `DealRecord` / `DealStage` — Mock → in Prod: HubSpot / Salesforce

```bash
POST /api/v1/agents/sales-copilot/process
```

---

### 5. Onboarding Agent (`agents/onboarding_agent.py`)

Startet den Onboarding-Prozess nach Vertragsabschluss: parsiert Kundendaten, baut eine personalisierte Checkliste, versendet die Willkommens-E-Mail und legt den Onboarding-Record an.

**Workflow:**
```
parse_customer_data → generate_checklist → compose_welcome_email → send_welcome → log_to_tracker → finalize
```

**Nodes:**

| Node | Art | Beschreibung |
|---|---|---|
| `parse_customer_data` | LLM | Extrahiert company, contact, email, plan, industry, team_size, primary_use_case, language |
| `generate_checklist` | **deterministisch** | `build_checklist(plan, industry)` — kein LLM-Aufruf |
| `compose_welcome_email` | LLM | Personalisierte Welcome-Mail mit SUBJECT:-Zeile, 3 konkreten ersten Schritten |
| `send_welcome` | `NotificationSystem` | Mock → in Prod: SendGrid / Postmark / AWS SES |
| `log_to_tracker` | `OnboardingTracker` | Mock → in Prod: HubSpot Onboarding-Pipeline / CS-System |
| `finalize` | — | Baut strukturierten Output |

**Checklisten-Logik (deterministisch, plan + industry):**

| Plan | Enthaltene Blöcke |
|---|---|
| `starter` | _BASE (5 Items: Account, Team, Kick-off, Integration, Quickstart) |
| `pro` | _BASE + _PRO (+4: Custom Domain, 2 Integrationen, SE-Call, erster Workflow) |
| `enterprise` | _BASE + _PRO + _ENTERPRISE (+5: CSM, Slack, SLA, SSO, Custom Training) |

Zusätzlich 1 Industry-Block wenn Branche erkannt: `healthcare` | `financial` | `manufacturing` | `e-commerce` | `logistics` | `real estate`

**Tools:**
- `OnboardingTracker` / `OnboardingRecord` / `ChecklistItem` — Mock → in Prod: CS-System / HubSpot
- `NotificationSystem` / `SentEmail` — Mock → in Prod: E-Mail-Provider

```bash
POST /api/v1/agents/onboarding/process
```

---

## Datei- und Modulstruktur

```
novara-agents/
├── main.py                         # FastAPI Gateway, Routing, Auth, File-Upload-Endpoint
├── requirements.txt
├── .env.example                    # Template – nie .env committen
├── CLAUDE.md                       # diese Datei
│
├── core/
│   ├── config.py                   # pydantic-settings, Singleton via lru_cache
│   ├── llm.py                      # Zentrale LLM-Factory + Demo-Modus-Fake-Client
│   └── security.py                 # DLP/PII-Redaktion, Hard-Block-Keywords
│
├── agents/
│   ├── base_agent.py               # BaseAgent, AgentRequest, AgentResponse
│   ├── onboarding_agent.py         # OnboardingGraph + OnboardingAgent
│   ├── operations_agent.py         # OperationsGraph + OperationsAgent
│   ├── sales_copilot_agent.py      # SalesCopilotGraph + SalesCopilotAgent
│   ├── sdr_agent.py                # SDRGraph + SDRAgent
│   └── support_agent.py            # SupportGraph + SupportAgent
│
└── tools/
    ├── crm_integration.py          # CRMIntegration (Rechnungen) + CRMIntegrationSDR (Leads)
    ├── deal_tracker.py             # DealTracker, DealRecord, DealStage
    ├── document_parser.py          # Regex-Extraktion, pdfplumber-Integration
    ├── faq_database.py             # FAQDatabase, 8 Einträge, Prefix-Stemming-Suche
    ├── lead_database.py            # LeadDatabase, 15 Mock-Kontakte, Fuzzy-Suche
    ├── notification_system.py      # NotificationSystem, SentEmail – Mock E-Mail-Versand
    ├── onboarding_tracker.py       # OnboardingTracker, build_checklist(), ChecklistItem
    └── ticket_system.py            # TicketSystem, TicketRecord, TicketPriority
```

---

## Einen neuen Agenten hinzufügen

1. **Agent-Klasse** in `agents/<name>_agent.py` anlegen — erbt von `BaseAgent`, implementiert `_run()`
2. **LangGraph-Graph** als innere Klasse (`<Name>Graph`) mit `StateGraph(TypedDict)` aufbauen
3. **Tools** in `tools/` ergänzen falls nötig, dann in `tools/__init__.py` exportieren
4. **Agent in Registry** in `main.py` unter `_build_registry()` eintragen
5. **Export** in `agents/__init__.py` ergänzen
6. Gemeinsames Pattern für LLM-JSON-Parsing: `_parse_llm_json(text)` im Agent definieren
   (strippt ` ```json ``` ` Code-Fences vor `json.loads`)

---

## Roadmap – nächste Schritte

Alle 5 Agenten sind implementiert. Mögliche Erweiterungen:

| Thema | Beschreibung |
|---|---|
| **Renewal Agent** | Erkennt Kunden mit niedrigem Health-Score vor Vertragsverlängerung und startet Rettungskampagne |
| **Churn-Detection** | Analysiert Nutzungsdaten und eskaliert an CSM wenn Aktivierungsgrad unter Schwellwert fällt |
| **Multi-Tenant Auth** | OAuth2 / JWT statt einfachem API-Key für SaaS-Mandantenfähigkeit |
| **Embedding-FAQ** | Vektor-Suche (Weaviate / pgvector) statt Keyword-Stemming für bessere FAQ-Treffer |
| **Prompt Caching** | Anthropic Prompt Caching für System-Prompts aktivieren (Kostensenkung ~90 % bei wiederholten Calls) |

---

## Bekannte Einschränkungen (Development-Modus)

| Einschränkung | Prod-Lösung |
|---|---|
| CRM / ERP = In-Memory-Mock | httpx-Client gegen HubSpot / Salesforce / SAP |
| FAQ-Suche = Keyword-Stemming | Embedding-Suche gegen Weaviate / Qdrant / pgvector |
| Lead-Datenbank = 15 Hard-coded-Kontakte | LinkedIn Sales Navigator API / CRM-Query |
| Ticket-System = Mock | Zendesk / Freshdesk / Jira Service Management API |
| LLM-Caching = keins | Anthropic Prompt Caching für wiederholte System-Prompts aktivieren |
| Kein Rate-Limiting | FastAPI `slowapi` Middleware ergänzen |
| Kein Auth außer API-Key | OAuth2 / JWT für Multi-Tenant-Szenarien |
