# Novara Agent Factory

Modulares Multi-Agenten-System für B2B-Prozessautomatisierung. Jeder Agent
kapselt einen eigenständigen Geschäftsprozess als LangGraph-Workflow und ist
über ein gemeinsames FastAPI-Gateway erreichbar.

Vollständige Architektur-/Modul-Dokumentation: siehe [`CLAUDE.md`](CLAUDE.md).

## Schnellstart

```bash
# 1. Abhängigkeiten installieren
pip install -r requirements.txt

# 2. Umgebungsvariablen setzen
cp .env.example .env
# → ANTHROPIC_API_KEY in .env eintragen

# 3. Server starten
python main.py
# oder direkt:
uvicorn main:app --reload --port 8000

# 4. Health-Check
curl http://localhost:8000/health
```

## Wichtige Umgebungsvariablen

Vollständiges Template: [`.env.example`](.env.example).

| Variable | Zweck |
|---|---|
| `ANTHROPIC_API_KEY` | LLM-Provider für alle 5 Factory-Agenten |
| `API_SECRET_KEY` | Auth für `/api/v1/agents/*` (`X-API-Key`-Header) |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | WhatsApp-Webhook (Baustellen-Voice-Assistant): Signaturprüfung + Media-Download |
| `GROQ_API_KEY` | Speech-to-Text (whisper-large-v3, [console.groq.com/keys](https://console.groq.com/keys)) für WhatsApp-Sprachnachrichten — ohne Key liefert `_transcribe_audio()` `None` und der Techniker bekommt eine Text-Fallback-Aufforderung |
| `SMTP_EMAIL` / `SMTP_PASSWORD` | Lead-Capture-Benachrichtigung per E-Mail |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` | `book_appointment`-Tool des Voice Agent |

## Tests

```bash
python test_system.py
```

## Deployment

Railway (`railway.toml`, Dockerfile-Build). `ENVIRONMENT=production` deaktiviert
Swagger UI und den `dev-secret`-API-Key-Bypass.
