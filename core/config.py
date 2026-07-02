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

    # Runtime
    environment: str = Field(default="development", alias="ENVIRONMENT")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def anthropic_key_configured(self) -> bool:
        """True, wenn ein echter ANTHROPIC_API_KEY gesetzt ist (kein Platzhalter)."""
        key = self.anthropic_api_key.get_secret_value().strip()
        return bool(key) and key != "mock-key"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
