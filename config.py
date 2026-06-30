import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).parent.absolute()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mongodb_uri: str = os.getenv("MONGODB_URI", "mongodb://localhost:27017/by8flow")
    database_name: str = os.getenv("DATABASE_NAME", "by8flow")
    jwt_secret: str = os.getenv("JWT_SECRET", "")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    access_token_expire_minutes: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "43200"))
    refresh_token_expire_days: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "40"))

    # SMTP
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from_name: str = os.getenv("SMTP_FROM_NAME", "By8flow")
    smtp_from_email: str = os.getenv("SMTP_FROM_EMAIL", "noreply@by8flow.com")
    # smtp_use_tls=True for port 465 (implicit TLS), False for port 587 (STARTTLS)
    smtp_use_tls: bool = os.getenv("SMTP_USE_TLS", "false").lower() == "true"
    # smtp_start_tls=True for port 587 (STARTTLS upgrade after plain connect)
    smtp_start_tls: bool = os.getenv("SMTP_START_TLS", "true").lower() == "true"
    # SMTP connection + operation timeout in seconds
    smtp_timeout: float = float(os.getenv("SMTP_TIMEOUT", "30.0"))

    # Resend HTTP email API — alternative to SMTP (works on Render Free tier)
    # Get a free key at https://resend.com — 3,000 emails/month free
    # When set, Resend is used as primary sender (SMTP becomes fallback)
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")


    # Mistral (legacy fallback)
    mistral_api_key: str = os.getenv("MISTRAL_API_KEY", "")

    # OpenRouter (primary AI provider)
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_default_model: str = os.getenv("OPENROUTER_DEFAULT_MODEL", "poolside/laguna-m.1:free")
    openrouter_site_url: str = os.getenv("OPENROUTER_SITE_URL", "https://by8flow.com")
    openrouter_site_name: str = os.getenv("OPENROUTER_SITE_NAME", "By8flow")

    # Frontend
    frontend_url: str = os.getenv("FRONTEND_URL", "http://localhost:3000")

    # Google Calendar OAuth2
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    google_calendar_id: str = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    google_redirect_uri: str = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/google-calendar/callback")

    # Google Drive OAuth2
    google_drive_client_id: str = os.getenv("GOOGLE_DRIVE_CLIENT_ID", "")
    google_drive_client_secret: str = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", "")
    google_drive_redirect_uri: str = os.getenv("GOOGLE_DRIVE_REDIRECT_URI", "http://localhost:8000/api/upload/callback")
    google_drive_refresh_token: str = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN", "")

    # Email Inbox (IMAP)
    encryption_key: str = os.getenv("ENCRYPTION_KEY", "")
    imap_host: str = os.getenv("IMAP_HOST", "imap.hostinger.com")
    imap_port: int = int(os.getenv("IMAP_PORT", "993"))

    # Redis Cache
    # REDIS_URL takes priority (full URL, e.g. rediss://... for Render managed Redis with TLS)
    redis_url: Optional[str] = os.getenv("REDIS_URL", None)
    redis_host: str = os.getenv("REDIS_HOST", "localhost")
    redis_port: int = int(os.getenv("REDIS_PORT", "6379"))
    redis_username: str = os.getenv("REDIS_USERNAME", "default")
    redis_password: str = os.getenv("REDIS_PASSWORD", "")
    redis_db: int = int(os.getenv("REDIS_DB", "0"))

    # Inbox Cache TTLs (seconds)
    inbox_cache_status_ttl: int = int(os.getenv("INBOX_CACHE_STATUS_TTL", "300"))
    inbox_cache_emails_ttl: int = int(os.getenv("INBOX_CACHE_EMAILS_TTL", "120"))
    inbox_cache_body_ttl: int = int(os.getenv("INBOX_CACHE_BODY_TTL", "1800"))
    inbox_cache_folders_ttl: int = int(os.getenv("INBOX_CACHE_FOLDERS_TTL", "600"))
    
    # GitHub OAuth
    github_client_id: str = os.getenv("GITHUB_CLIENT_ID", "")
    github_client_secret: str = os.getenv("GITHUB_CLIENT_SECRET", "")
    backend_url: str = os.getenv("BACKEND_URL", "http://localhost:8000")

    # Microsoft Graph / Teams
    msal_client_id: str = os.getenv("MSAL_CLIENT_ID", "")
    msal_tenant_id: str = os.getenv("MSAL_TENANT_ID", "")
    msal_client_secret: str = os.getenv("MSAL_CLIENT_SECRET", "")
    graph_api_endpoint: str = os.getenv("GRAPH_API_ENDPOINT", "https://graph.microsoft.com/v1.0")

    # SSL/HTTPS
    ssl_keyfile: Optional[str] = os.getenv("SSL_KEYFILE")
    ssl_certfile: Optional[str] = os.getenv("SSL_CERTFILE")


@lru_cache
def get_settings() -> Settings:
    return Settings()