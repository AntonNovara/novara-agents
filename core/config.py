from functools import lru_cache
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM
    anthropic_api_key: SecretStr = Field(default="mock-key", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL")

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def strip_api_key(cls, v: object) -> object:
        """Entfernt trailing Whitespace/Newlines aus dem API-Key (Railway copy-paste Fehler)."""
        return v.strip() if isinstance(v, str) else v

    # API Security
    api_secret_key: SecretStr = Field(default="dev-secret", alias="API_SECRET_KEY")
    api_key_header: str = Field(default="X-API-Key", alias="API_KEY_HEADER")

    # CRM / ERP
    crm_endpoint: str = Field(default="https://crm.mock/api/v1", alias="CRM_ENDPOINT")
    crm_api_key: SecretStr = Field(default="mock-key", alias="CRM_API_KEY")

    # Landing-Page-Chat-Widget (Inbound-SDR, siehe agents/sdr_agent.py
    # InboundChatGraph): Termin-Link, der einem als ICP qualifizierten
    # Besucher zusammen mit should_book_demo=true zurückgegeben wird.
    # Default = derselbe reale Link wie in novara_wissen.txt
    # ("TERMINBUCHUNG: https://calendar.app.google/Dqmz7HkW2XNktT6q6").
    demo_booking_url: str = Field(
        default="https://calendar.app.google/Dqmz7HkW2XNktT6q6", alias="DEMO_BOOKING_URL"
    )

    # Google Calendar (book_appointment tool)
    google_client_id: SecretStr = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: SecretStr = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_refresh_token: SecretStr = Field(default="", alias="GOOGLE_REFRESH_TOKEN")

    # DSGVO
    data_residency_region: str = Field(default="eu-central-1", alias="DATA_RESIDENCY_REGION")
    enable_pii_redaction: bool = Field(default=True, alias="ENABLE_PII_REDACTION")

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="json", alias="LOG_FORMAT")

    # Post-Call Export (Claude Cowork Brücke)
    calls_export_dir: str = Field(default="~/novara-calls", alias="CALLS_EXPORT_DIR")

    # Runtime
    environment: str = Field(default="development", alias="ENVIRONMENT")

    # Demo-Modus: erzwingt Fake-LLM-Antworten statt echter API-Calls, auch wenn
    # ein echter ANTHROPIC_API_KEY gesetzt ist. Aktuell reine Entwicklungs-/
    # Kosten-Bequemlichkeit für den internen Gebrauch (lokale Tests ohne echte
    # Calls) – KEIN Sicherheits-Mechanismus. Sobald ein Agent öffentlich als
    # Demo exponiert wird, muss dort ein echter Fail-Safe-Default eingeführt
    # werden (Demo-Modus an, sofern nicht explizit für Prod freigeschaltet) –
    # das ist bewusst noch nicht Teil dieses Flags.
    demo_mode: bool = Field(default=False, alias="DEMO_MODE")

    # Voice Agent Sicherheits-Gate: VoiceAgent (agents/voice_agent.py) hat
    # während des laufenden Live-Telefongesprächs KEINE DLP-Schicht -- siehe
    # CLAUDE.md, Abschnitt "Voice Agent". Muss explizit auf true gesetzt
    # werden, sonst verweigert VoiceAgent den Start. Verhindert, dass eine
    # (versehentliche oder kontextlose) Reaktivierung des Voice-Service auf
    # Railway stillschweigend wieder live geht, ohne dass jemand diese Lücke
    # bewusst geprüft hat.
    voice_agent_dlp_reviewed: bool = Field(default=False, alias="VOICE_AGENT_DLP_REVIEWED")

    # SDR → CRM-Sheet-Kopplung (Block C1): Live-Schreibzugriff auf das
    # Produktions-Google-Sheet in la-maquina-de-confianza/crm_handler.py ist
    # per Default AUS (fail-safe, analog zu voice_agent_dlp_reviewed) — ohne
    # dieses Flag bleibt write_to_crm beim harmlosen In-Memory-Mock
    # (tools/crm_integration.py). NUR für lokale Entwicklung gedacht:
    # crm_handler.py liegt in einem Schwester-Repo mit eigenem, an DIESEN Mac
    # gebundenem OAuth-Token — ein Railway-Deploy von novara-agents hat
    # keinen Zugriff darauf (siehe tools/live_crm_bridge.py, CLAUDE.md).
    sdr_crm_live_sheet: bool = Field(default=False, alias="SDR_CRM_LIVE_SHEET")
    la_maquina_de_confianza_path: str = Field(
        default="../la-maquina-de-confianza", alias="LA_MAQUINA_DE_CONFIANZA_PATH"
    )

    # Support Agent: welchen Wissens-Mandanten laden (siehe core/knowledge.py,
    # load_wissen). Default "novara" = bisheriges Verhalten unverändert.
    support_knowledge_client: str = Field(default="novara", alias="SUPPORT_KNOWLEDGE_CLIENT")

    # Support Agent: echte Eskalations-E-Mail bei create_ticket. Analog zu
    # sdr_crm_live_sheet fail-safe AUS per Default — ohne dieses Flag bleibt
    # es beim reinen In-Memory-Mock (tools/ticket_system.py), es wird nie
    # eine echte E-Mail verschickt. Nutzt dieselbe Gmail-OAuth-Brücke zum
    # Schwester-Repo la-maquina-de-confianza wie sdr_crm_live_sheet
    # (tools/email_sender.py), sendet also immer als das dort hinterlegte
    # Konto (aktuell anton@novaraautomation.com), nie "als" der Kunde.
    support_escalation_email_live: bool = Field(default=False, alias="SUPPORT_ESCALATION_EMAIL_LIVE")
    support_escalation_email_to: str = Field(default="", alias="SUPPORT_ESCALATION_EMAIL_TO")

    # Lead-Capture-Benachrichtigung (core/lead_capture.py, tools/lead_notifier.py):
    # Gmail-Anwendungspasswort für den SMTP-Versand, sobald ein Website-Chat-
    # oder Voice-Besucher Kontaktdaten preisgibt. Bewusst eigenständige,
    # einfache SMTP-Credentials statt der bestehenden Gmail-OAuth-Brücke
    # (tools/email_sender.py) — die ist an ein lokales, an diesen Mac
    # gebundenes OAuth-Token geknüpft und funktioniert NICHT auf Railway;
    # SMTP_EMAIL/SMTP_PASSWORD sind normale Env-Vars, die auf jedem
    # Deployment (auch Railway) gesetzt werden können. Ohne beide Werte
    # überspringt send_lead_notification() den Versand (fail-safe, kein
    # Absturz des Chat-/Voice-Antwortpfads).
    smtp_email: SecretStr = Field(default="", alias="SMTP_EMAIL")
    smtp_password: SecretStr = Field(default="", alias="SMTP_PASSWORD")

    # GuardianAgent (agents/guardian_agent.py) — Health & Infrastructure
    # Audit (GET /api/v1/health/audit). Default = die Netlify-eigene
    # Subdomain, NICHT die Custom Domain novaraautomation.com: deren DNS
    # ist derzeit nicht auf Netlify delegiert (Registrar-Nameserver ohne
    # A/CNAME-Records, siehe Session-Notiz 17.09.2026) und würde den Audit
    # fälschlich als "Netlify down" melden, obwohl nur die Registrar-DNS
    # des Kunden kaputt ist — die *.netlify.app-Subdomain ist von diesem
    # DNS-Problem unabhängig und der zuverlässigere Indikator für "ist das
    # Netlify-Deployment selbst erreichbar".
    netlify_site_url: str = Field(
        default="https://novara-automation.netlify.app", alias="NETLIFY_SITE_URL"
    )

    # Baustellen-Voice-Assistant (main.py POST /api/v1/webhook/whatsapp,
    # agents/field_worker_agent.py): Twilio-Zugangsdaten für (a)
    # Signaturprüfung eingehender Webhook-Requests (RequestValidator,
    # X-Twilio-Signature) und (b) authentifizierten Download von
    # WhatsApp-Sprachnachrichten (Twilio-Media-URLs verlangen HTTP-Basic-Auth
    # mit genau diesen beiden Werten). Ohne twilio_auth_token wird die
    # Signaturprüfung übersprungen (mit Warn-Log) statt den Webhook hart zu
    # blocken -- dieselbe Fail-Safe-für-lokale-Entwicklung-Philosophie wie
    # ANTHROPIC_API_KEY/Demo-Modus (core/llm.py), NICHT für Produktivbetrieb
    # gedacht: dort MUSS twilio_auth_token gesetzt sein, sonst nimmt der
    # Endpoint unauthentifizierte Requests an, die echte LLM-Calls und
    # PDF-Generierung auslösen (Ressourcen-/Spam-Risiko, siehe
    # CLAUDE.md-Abschnitt zum Baustellen-Voice-Assistant).
    twilio_account_sid: SecretStr = Field(default="", alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: SecretStr = Field(default="", alias="TWILIO_AUTH_TOKEN")

    # Baustellen-Voice-Assistant (main.py _transcribe_audio()): Groq-API-Key
    # für Speech-to-Text (whisper-large-v3) von WhatsApp-Sprachnachrichten.
    # Ohne Key liefert _transcribe_audio() None (fail-safe, kein Absturz) --
    # derselbe Umgang mit fehlender Konfiguration wie bei twilio_auth_token.
    groq_api_key: SecretStr = Field(default="", alias="GROQ_API_KEY")

    # Persistenter Store (core/db.py) für die vier zuvor In-Memory-Prozess-
    # Singletons (core/consent.py, core/customer_state.py, core/lead_capture.py,
    # tools/sequence_scheduler.py) -- Railway setzt DATABASE_URL automatisch,
    # sobald ein Postgres-Plugin an diesen Service angehängt ist. Ohne
    # DATABASE_URL (lokale Entwicklung ohne eigenes Postgres) fällt core/db.py
    # auf eine lokale SQLite-Datei zurück -- siehe dortigen Kommentar.
    database_url: str = Field(default="", alias="DATABASE_URL")

    @property
    def groq_key_configured(self) -> bool:
        """True, wenn ein echter GROQ_API_KEY gesetzt ist."""
        return bool(self.groq_api_key.get_secret_value().strip())

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def anthropic_key_configured(self) -> bool:
        """True, wenn ein echter ANTHROPIC_API_KEY gesetzt ist (kein Platzhalter)."""
        key = self.anthropic_api_key.get_secret_value().strip()
        return bool(key) and key != "mock-key"

    @property
    def effective_demo_mode(self) -> bool:
        """
        True, wenn LLM-Calls durch Fake-Antworten ersetzt werden sollen:
        entweder explizit über DEMO_MODE=true, oder implizit, weil kein
        echter API-Key konfiguriert ist (verhindert Abstürze bei fehlendem Key).
        """
        return self.demo_mode or not self.anthropic_key_configured


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
