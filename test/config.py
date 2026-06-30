from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path

BACKEND_DIR = Path(__file__).parent.absolute()


class Settings(BaseSettings):
    mongodb_uri: str = "mongodb://localhost:27017/by8flow"
    database_name: str = "by8flow"
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 43200  # 30 days
    refresh_token_expire_days: int = 40

    # SMTP
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_name: str = "By8flow"
    smtp_from_email: str = "noreply@by8flow.com"

    # Mistral (legacy fallback)
    mistral_api_key: str = ""

    # OpenRouter (primary AI provider)
    openrouter_api_key: str = ""
    openrouter_default_model: str = "poolside/laguna-m.1:free"
    openrouter_site_url: str = "https://by8flow.com"
    openrouter_site_name: str = "By8flow"

    # Frontend
    frontend_url: str = "http://localhost:3000"

    # Google Calendar OAuth2
    google_client_id: str = ""
    google_client_secret: str = ""
    google_calendar_id: str = "primary"
    google_redirect_uri: str = "http://localhost:8000/api/google-calendar/callback"

    # Google Drive OAuth2
    google_drive_client_id: str = ""
    google_drive_client_secret: str = ""
    google_drive_redirect_uri: str = "http://localhost:8000/api/upload/callback"
    google_drive_refresh_token: str = ""

    # Email Inbox (IMAP)
    encryption_key: str = ""
    imap_host: str = "imap.hostinger.com"
    imap_port: int = 993

    # Redis Cache
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_username: str = "default"
    redis_password: str = ""
    redis_db: int = 0

    # Inbox Cache TTLs (seconds)
    inbox_cache_status_ttl: int = 300        # 5 minutes
    inbox_cache_emails_ttl: int = 120        # 2 minutes
    inbox_cache_body_ttl: int = 1800         # 30 minutes
    inbox_cache_folders_ttl: int = 600       # 10 minutes
    
    # GitHub OAuth
    github_client_id: str = ""
    github_client_secret: str = ""
    backend_url: str = "http://localhost:8000"

    # Microsoft Graph / Teams
    msal_client_id: str = ""
    msal_tenant_id: str = ""
    msal_client_secret: str = ""
    graph_api_endpoint: str = "https://graph.microsoft.com/v1.0"

    class Config:
        env_file = str(BACKEND_DIR / ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
