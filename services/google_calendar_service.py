"""Google Calendar integration service."""
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import logging

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from database import get_database
from bson import ObjectId
from config import get_settings
from utils.encryption import encrypt_password, decrypt_password

logger = logging.getLogger(__name__)

# OAuth2 scopes
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]


class GoogleCalendarService:
    """Service for interacting with Google Calendar API."""

    def __init__(self):
        settings = get_settings()
        self.client_id = settings.google_client_id
        self.client_secret = settings.google_client_secret
        self.calendar_id = settings.google_calendar_id

    async def _get_tokens(self, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get stored OAuth tokens from database for a specific user or global."""
        db = get_database()
        if db is None:
            logger.error("Database connection is None (db not initialized)")
            return None
        # The oauth_tokens collection uses a custom string _id schema to support multiple OAuth integrations:
        # - Per-user Google Calendar tokens use _id = f"google_calendar_{user_id}"
        # - Global/system Google Calendar tokens use _id = "google_calendar"
        # This prevents collisions and allows single-key point queries without complex secondary indexes.
        doc_id = f"google_calendar_{user_id}" if user_id else "google_calendar"
        try:
            token_doc = await db["oauth_tokens"].find_one({"_id": doc_id})
            if token_doc:
                # Decrypt stored tokens
                if token_doc.get("access_token_enc"):
                    try:
                        token_doc["access_token"] = decrypt_password(token_doc["access_token_enc"])
                    except Exception as e:
                        logger.error(f"[_get_tokens] Failed to decrypt access_token: {e}")
                        return None
                if token_doc.get("refresh_token_enc"):
                    try:
                        token_doc["refresh_token"] = decrypt_password(token_doc["refresh_token_enc"])
                    except Exception as e:
                        logger.error(f"[_get_tokens] Failed to decrypt refresh_token: {e}")
                        return None
                refresh_present = "yes" if token_doc.get("refresh_token") else "NO"
                expiry = token_doc.get("token_expiry")
                logger.info(f"[_get_tokens] Found token doc for {doc_id}: access_token={'YES' if token_doc.get('access_token') else 'NO'}, refresh_token={refresh_present}, token_expiry={expiry}")
                return token_doc
            else:
                logger.warning(f"[_get_tokens] No token document found for _id='{doc_id}'")
                return None
        except Exception as e:
            logger.error(f"[_get_tokens] Error fetching tokens: {e}", exc_info=True)
            return None

    async def _store_tokens(self, tokens: Dict[str, Any], user_id: Optional[str] = None):
        """Store OAuth tokens in database for a specific user or global."""
        db = get_database()
        doc_id = f"google_calendar_{user_id}" if user_id else "google_calendar"
        access_token_enc = encrypt_password(tokens.get("access_token", "")) if tokens.get("access_token") else ""
        refresh_token_enc = encrypt_password(tokens.get("refresh_token", "")) if tokens.get("refresh_token") else ""
        update_data = {
            "$set": {
                "access_token_enc": access_token_enc,
                "refresh_token_enc": refresh_token_enc,
                "updated_at": datetime.now(timezone.utc),  # timezone-aware UTC
            }
        }
        # Only set token_expiry if provided
        if tokens.get("expiry") is not None:
            update_data["$set"]["token_expiry"] = tokens.get("expiry")
        await db["oauth_tokens"].update_one(
            {"_id": doc_id},
            update_data,
            upsert=True,
        )

    async def _build_credentials(self, token_doc: Optional[Dict] = None, user_id: Optional[str] = None) -> Optional[Credentials]:
        """Build Google Credentials object from stored tokens."""
        if not token_doc:
            logger.warning("_build_credentials: No token document provided")
            return None

        access_token = token_doc.get("access_token")
        refresh_token = token_doc.get("refresh_token")
        token_expiry = token_doc.get("token_expiry")

        logger.info(f"_build_credentials: access_token={'YES' if access_token else 'NO'}, refresh_token={'YES' if refresh_token else 'NO'}, token_expiry={token_expiry}")

        try:
            creds = Credentials(
                token=access_token,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=self.client_id,
                client_secret=self.client_secret,
                scopes=SCOPES,
            )
        except Exception as e:
            logger.error(f"_build_credentials: Failed to create Credentials object: {e}", exc_info=True)
            return None

        # Set expiry from stored token_expiry if available
        if token_expiry:
            try:
                if isinstance(token_expiry, datetime):
                    parsed = token_expiry
                else:
                    # Parse ISO string. Could be naive or aware.
                    parsed = datetime.fromisoformat(token_expiry)
                # Google's Credentials expects naive UTC datetime for expiry
                if parsed.tzinfo is not None:
                    # Convert aware to naive UTC
                    parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
                creds.expiry = parsed
                logger.info(f"_build_credentials: Set creds.expiry = {creds.expiry} (naive UTC)")
            except (ValueError, TypeError) as e:
                logger.warning(f"_build_credentials: Invalid token_expiry format '{token_expiry}': {e}")
        else:
            logger.warning("_build_credentials: No token_expiry in document; creds.expiry will be None")

        # Debug: check expiry state
        if creds.expiry:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            is_exp = now >= creds.expiry
            logger.info(f"_build_credentials: Now={now}, Expiry={creds.expiry}, expired={is_exp}")
        else:
            logger.info("_build_credentials: creds.expiry is None, creds.expired will be False")

        # Refresh if expired
        if creds.expired and creds.refresh_token:
            logger.info("_build_credentials: Token expired, attempting refresh...")
            try:
                creds.refresh(Request())
                logger.info("_build_credentials: Refresh succeeded, new access_token obtained")
                # Update stored tokens
                await self._store_tokens({
                    "access_token": creds.token,
                    "refresh_token": creds.refresh_token or token_doc.get("refresh_token"),
                    "expiry": creds.expiry.isoformat() if creds.expiry else None,
                }, user_id)
            except Exception as e:
                logger.error(f"_build_credentials: Failed to refresh token: {e}", exc_info=True)
                return None
        elif creds.expired:
            logger.warning("_build_credentials: Token expired but no refresh_token available")
            return None

        logger.info(f"_build_credentials: Returning credentials: valid={creds.valid if hasattr(creds, 'valid') else 'N/A'}")
        return creds

    async def is_connected(self, user_id: Optional[str] = None) -> bool:
        """Check if Google Calendar is connected and tokens are valid for a user."""
        logger.info(f"is_connected() check started for user_id={user_id}")
        token_doc = await self._get_tokens(user_id)
        if not token_doc:
            logger.warning("is_connected: No token document found")
            return False
        if not token_doc.get("refresh_token") and not token_doc.get("refresh_token_enc"):
            logger.warning(f"is_connected: No refresh_token in token document. Keys: {list(token_doc.keys())}")
            return False

        logger.info(f"is_connected: Token document found, building credentials...")
        creds = await self._build_credentials(token_doc, user_id)
        if creds is None:
            logger.warning("is_connected: _build_credentials returned None")
            return False
        if not creds.valid:
            # creds.expired may be true, but we want to know why it's not valid
            logger.warning(f"is_connected: Credentials not valid. expired={creds.expired}, has_refresh={bool(creds.refresh_token)}")
            return False

        logger.info("is_connected: Credentials valid, connected=True")
        return True

    async def build_service(self, user_id: Optional[str] = None):
        """Build Google Calendar API service for a specific user or global."""
        logger.info(f"Building Google Calendar service for user_id={user_id}")
        token_doc = await self._get_tokens(user_id)
        if not token_doc:
            # Additional diagnostic: try to check if collection exists and count
            db = get_database()
            if db is not None:
                try:
                    count = await db["oauth_tokens"].count_documents({})
                    logger.error(f"No token document found. Collection oauth_tokens exists with {count} documents.")
                except Exception as e:
                    logger.error(f"Failed to count oauth_tokens: {e}")
            raise Exception("Google Calendar not connected. No OAuth tokens found.")

        # Log redacted token info
        logger.info(f"Token document: access_token={'YES' if token_doc.get('access_token_enc') else 'NO'}, "
                    f"refresh_token={'YES' if token_doc.get('refresh_token_enc') else 'NO'}, "
                    f"token_expiry={token_doc.get('token_expiry')}, "
                    f"client_id={'YES' if token_doc.get('client_id') else 'NO'}")
        creds = await self._build_credentials(token_doc, user_id)
        if not creds:
            logger.error("Failed to build Google Calendar credentials: credentials are None after _build_credentials")
            raise Exception("Failed to build Google Calendar credentials.")

        try:
            service = build("calendar", "v3", credentials=creds)
            logger.info("Google Calendar service built successfully")
            return service
        except Exception as e:
            logger.error(f"Failed to build Google Calendar service: {e}")
            raise

    async def create_leave_event(
        self,
        leave_id: str,
        user_name: str,
        leave_type: str,
        start_date: str,
        end_date: str,
        reason: str,
        days: int,
    ) -> str:
        """
        Create an all-day event in Google Calendar for approved leave.

        Returns: Google Calendar event ID
        """
        try:
            service = await self.build_service()

            # Format dates (all-day event uses date only, no time)
            start = start_date  # already in YYYY-MM-DD
            
            # Google Calendar end.date is exclusive, so add 1 day to end_date
            from datetime import date, timedelta
            try:
                end_dt = date.fromisoformat(end_date)
                end = (end_dt + timedelta(days=1)).isoformat()
            except Exception as e:
                logger.warning(f"Failed to parse end_date '{end_date}' for exclusive increment: {e}")
                end = end_date

            event = {
                "summary": f"{user_name} - {leave_type.title()} Leave ({days} days)",
                "description": f"Leave Request\nType: {leave_type}\nDays: {days}\nReason: {reason}\nLeave ID: {leave_id}",
                "start": {
                    "date": start,
                },
                "end": {
                    "date": end,
                },
                "colorId": "9" if leave_type == "annual" else "6",  # blue=9, orange=6
                "reminders": {
                    "useDefault": False,
                    "overrides": [],
                },
            }

            created = (
                service.events()
                .insert(calendarId=self.calendar_id, body=event)
                .execute()
            )

            logger.info(f"Created Google Calendar event {created['id']} for leave {leave_id}")
            return created["id"]

        except HttpError as e:
            logger.error(f"Google Calendar API error: {e}")
            raise Exception(f"Failed to create calendar event: {e}")
        except Exception as e:
            logger.error(f"Unexpected error creating event: {e}")
            raise

    async def delete_leave_event(self, google_event_id: str):
        """Delete a Google Calendar event."""
        try:
            service = await self.build_service()
            service.events().delete(
                calendarId=self.calendar_id,
                eventId=google_event_id,
            ).execute()
            logger.info(f"Deleted Google Calendar event {google_event_id}")
        except HttpError as e:
            if e.resp.status == 404:
                logger.warning(f"Event {google_event_id} not found in Google Calendar")
            else:
                logger.error(f"Failed to delete Google Calendar event: {e}")
                raise

    async def get_events(self, start_date: str, end_date: str, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fetch Google Calendar events for a date range for a specific user.

        Args:
            start_date: YYYY-MM-DD
            end_date: YYYY-MM-DD
            user_id: User ID for per-user tokens

        Returns:
            List of event dicts with keys: id, summary, start, end, etc.
        """
        try:
            service = await self.build_service(user_id)

            # Fetch events in time range
            cal_id = "primary" if user_id else self.calendar_id
            events_result = (
                service.events()
                .list(
                    calendarId=cal_id,
                    timeMin=f"{start_date}T00:00:00Z",
                    timeMax=f"{end_date}T23:59:59Z",
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )

            events = events_result.get("items", [])
            formatted = []
            for event in events:
                formatted.append({
                    "id": event["id"],
                    "summary": event.get("summary", ""),
                    "description": event.get("description", ""),
                    "start": event["start"].get("date", event["start"].get("dateTime", "")),
                    "end": event["end"].get("date", event["end"].get("dateTime", "")),
                    "colorId": event.get("colorId"),
                })

            logger.info(f"Fetched {len(formatted)} Google Calendar events")
            return formatted

        except HttpError as e:
            logger.error(f"Google Calendar API error fetching events: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching Google Calendar events: {e}", exc_info=True)
            return []

    async def check_connection(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Check and return connection status for a user."""
        logger.info(f"check_connection() called for user_id={user_id}")
        try:
            connected = await self.is_connected(user_id)
            logger.info(f"is_connected() returned: {connected}")
            return {
                "connected": connected,
                "calendar_id": ("primary" if user_id else self.calendar_id) if connected else None,
            }
        except Exception as e:
            logger.error(f"check_connection() error: {e}", exc_info=True)
            return {"connected": False, "calendar_id": None}

    async def create_meeting_event(
        self,
        title: str,
        start_datetime: str,  # ISO format: "2026-04-30T10:00:00Z"
        end_datetime: str,
        description: str = "",
        attendees: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """
        Create a Google Calendar event with a Google Meet link.

        Args:
            title: Meeting title
            start_datetime: ISO datetime string (with Z for UTC)
            end_datetime: ISO datetime string (with Z for UTC)
            description: Optional description/agenda
            attendees: Optional list of email addresses
            user_id: Optional user ID of the creator

        Returns:
            dict with keys: meet_link (str), event_id (str)
        """
        try:
            import uuid
            service = await self.build_service(user_id)

            # Build attendees list
            attendees_list = []
            if attendees:
                for email in attendees:
                    attendees_list.append({"email": email})

            event = {
                "summary": title,
                "description": description,
                "start": {
                    "dateTime": start_datetime,
                    "timeZone": "UTC",
                },
                "end": {
                    "dateTime": end_datetime,
                    "timeZone": "UTC",
                },
                "conferenceData": {
                    "createRequest": {
                        "requestId": f"meet-{uuid.uuid4()}",
                        "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    }
                },
            }

            # Only include attendees if provided (no auto-invite policy)
            if attendees_list:
                event["attendees"] = attendees_list

            cal_id = "primary" if user_id else self.calendar_id
            created = (
                service.events()
                .insert(
                    calendarId=cal_id,
                    body=event,
                    conferenceDataVersion=1,
                    sendUpdates="all",
                )
                .execute()
            )

            # Extract Meet link from conferenceData
            meet_link = ""
            if "conferenceData" in created and "entryPoints" in created["conferenceData"]:
                for entry in created["conferenceData"]["entryPoints"]:
                    if entry.get("entryPointType") == "video":
                        meet_link = entry["uri"]
                        break
                # Fallback to first entry point
                if not meet_link and created["conferenceData"]["entryPoints"]:
                    meet_link = created["conferenceData"]["entryPoints"][0]["uri"]

            logger.info(f"Created Google Meet event: {created['id']} with link: {meet_link}")

            return {
                "meet_link": meet_link,
                "event_id": created["id"],
            }

        except HttpError as e:
            logger.error(f"Google Calendar API error creating meeting: {e}")
            raise Exception(f"Failed to create Google Meet: {e}")
        except Exception as e:
            logger.error(f"Unexpected error creating meeting: {e}")
            raise

    async def delete_meeting_event(self, event_id: str, user_id: Optional[str] = None) -> None:
        """Delete a Google Calendar event by ID."""
        try:
            service = await self.build_service(user_id)
            cal_id = "primary" if user_id else self.calendar_id
            service.events().delete(
                calendarId=cal_id,
                eventId=event_id,
                sendUpdates="all"
            ).execute()
            logger.info(f"Deleted Google Calendar event: {event_id}")
        except Exception as e:
            logger.error(f"Failed to delete Google Calendar event {event_id}: {e}", exc_info=True)

    async def disconnect(self, user_id: Optional[str] = None):
        """Revoke OAuth access and clear stored tokens for a user."""
        logger.info(f"Disconnecting Google Calendar for user_id={user_id}")
        token_doc = await self._get_tokens(user_id)
        if not token_doc:
            logger.info("No token document found, nothing to disconnect")
            return  # Nothing to disconnect

        # Try to revoke token via Google if we have a refresh token
        if token_doc.get("refresh_token"):
            try:
                import requests
                refresh_token = token_doc.get("refresh_token")
                client_id = self.client_id
                client_secret = self.client_secret

                logger.info(f"Attempting revocation with client_id: {client_id[:30] if client_id else 'None'}...")
                response = requests.post(
                    "https://oauth2.googleapis.com/revoke",
                    data={
                        "token": refresh_token,
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                    headers={"content-type": "application/x-www-form-urlencoded"},
                )
                if response.status_code != 200:
                    logger.warning(f"Revocation returned {response.status_code}: {response.text[:200]}")
                    # Don't raise — we'll still clear local tokens
                else:
                    logger.info("Google OAuth token revoked successfully")
            except Exception as e:
                logger.warning(f"Failed to revoke token: {e}")

        # Always clear token document from DB (even if revocation failed)
        db = get_database()
        doc_id = f"google_calendar_{user_id}" if user_id else "google_calendar"
        result = await db["oauth_tokens"].delete_one({"_id": doc_id})
        logger.info(f"Token document deleted: {result.deleted_count} document(s) removed")


# Singleton instance
google_calendar_service = GoogleCalendarService()
