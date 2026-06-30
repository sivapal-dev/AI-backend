from config import get_settings

settings = get_settings()
print("=== Google OAuth Settings ===")
print(f"google_client_id: {settings.google_client_id[:50] if settings.google_client_id else 'EMPTY'}")
print(f"google_client_secret: {settings.google_client_secret[:20] if settings.google_client_secret else 'EMPTY'}")
print(f"google_calendar_id: {settings.google_calendar_id}")
print(f"google_redirect_uri: {settings.google_redirect_uri}")
