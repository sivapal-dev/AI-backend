"""Google Calendar OAuth2 endpoints."""
import asyncio
import os
import json
import secrets
import urllib.parse
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import requests

from dependencies import get_current_user, get_current_active_user
from database import get_database
from services.google_calendar_service import google_calendar_service
from config import get_settings
from utils.encryption import encrypt_password


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/google-calendar", tags=["google-calendar"])

# Get settings once at module load
settings = get_settings()


class ConnectResponse(BaseModel):
    """Response for connect endpoint."""
    url: str
    state: str


class StatusResponse(BaseModel):
    """Response for status endpoint."""
    connected: bool
    calendar_id: Optional[str] = None


class DisconnectResponse(BaseModel):
    """Response for disconnect endpoint."""
    message: str


@router.post("/connect")
async def connect_google_calendar(
    current_user: dict = Depends(get_current_active_user),
):
    """
    Start OAuth2 flow to connect Google Calendar.

    Each user connects their own Google Calendar.
    Returns a URL to redirect the user to Google's consent screen.
    """
    user_id = str(current_user["id"])
    # Generate state token for CSRF protection
    state = secrets.token_urlsafe(32)

    # Store state in DB temporarily (expires in 10 minutes)
    db = get_database()
    await db["oauth_states"].insert_one({
        "_id": state,
        "user_id": str(current_user["id"]),  # user dict has 'id' not '_id'
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
    })

    # Build OAuth2 URL
    client_id = settings.google_client_id
    redirect_uri = settings.google_redirect_uri

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join([
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.readonly",
        ]),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }

    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"

    return {"url": auth_url, "state": state}


@router.get("/callback")
async def google_calendar_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    """
    OAuth2 callback endpoint.

    Google redirects here after user consents. We exchange the code for tokens.
    """
    # Verify state
    db = get_database()
    state_doc = await db["oauth_states"].find_one({
        "_id": state,
        "expires_at": {"$gt": datetime.now(timezone.utc)},
    })
    if not state_doc:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    # Clean up state
    await db["oauth_states"].delete_one({"_id": state})

    # Exchange code for tokens (use thread pool to avoid blocking)
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret
    redirect_uri = settings.google_redirect_uri

    token_response = await asyncio.to_thread(
        requests.post,
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if token_response.status_code != 200:
        logger.error(f"Token exchange failed: {token_response.text}")
        raise HTTPException(status_code=400, detail="Failed to exchange authorization code")

    tokens = token_response.json()
    logger = logging.getLogger(__name__)  # Get logger for callback
    logger.info(f"Token exchange successful. Tokens keys: {list(tokens.keys())}")

    # Calculate token expiry time from expires_in (Google returns seconds)
    expires_in = tokens.get("expires_in", 3600)  # default 1 hour
    # Use naive UTC datetime (no timezone) — Google expects naive datetimes
    token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)
    logger.info(f"Token expiry set to (naive UTC): {token_expiry.isoformat()}")

    user_id = state_doc.get("user_id", "")

    # Encrypt tokens before storing
    access_token_enc = encrypt_password(tokens.get("access_token", ""))
    refresh_token_enc = encrypt_password(tokens.get("refresh_token", "")) if tokens.get("refresh_token") else ""

    # Store tokens in database per-user
    doc_id = f"google_calendar_{user_id}" if user_id else "google_calendar"
    result = await db["oauth_tokens"].update_one(
        {"_id": doc_id},
        {
            "$set": {
                "access_token_enc": access_token_enc,
                "refresh_token_enc": refresh_token_enc,
                "token_expiry": token_expiry.isoformat(),  # naive ISO string
                "updated_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )
    logger.info(f"Token storage result: matched={result.matched_count}, modified={result.modified_count}, upserted_id={result.upserted_id}")

    # Redirect back to frontend settings page with success flag
    frontend_url = settings.frontend_url
    return RedirectResponse(url=f"{frontend_url}/dashboard/settings?google_calendar=connected")


@router.get("/status", response_model=StatusResponse)
async def get_connection_status(
    current_user: dict = Depends(get_current_active_user),
):
    """Check if Google Calendar is connected for the current user."""
    try:
        status = await google_calendar_service.check_connection(str(current_user["id"]))
        return status
    except Exception as e:
        logger.error(f"Error in get_connection_status: {e}", exc_info=True)
        return {"connected": False}


@router.post("/disconnect", response_model=DisconnectResponse)
async def disconnect_google_calendar(
    current_user: dict = Depends(get_current_active_user),
):
    """Disconnect Google Calendar and revoke access for the current user."""
    await google_calendar_service.disconnect(str(current_user["id"]))
    return {"message": "Google Calendar disconnected successfully"}


@router.get("/events")
async def fetch_google_events(
    start_date: str,
    end_date: str,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Fetch Google Calendar events for a date range.

    Used by the team calendar view to show both leave and Google events.
    Available to all authenticated users.
    """
    events = await google_calendar_service.get_events(start_date, end_date, str(current_user["id"]))
    return {"events": events}


class SyncHolidaysRequest(BaseModel):
    year: int


@router.post("/sync-holidays")
async def sync_holidays_from_google(
    body: SyncHolidaysRequest,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Sync all-day Google Calendar events as holidays.

    Admin only. Reads all-day events from connected Google Calendar
    and imports them as public holidays.
    """
    if current_user.get("role", "").lower() != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin can sync holidays",
        )

    year = body.year
    start = f"{year}-01-01"
    end = f"{year}-12-31"

    events = await google_calendar_service.get_events(start, end)

    db = get_database()
    synced = 0
    for event in events:
        start_raw = event.get("start", "")
        if not start_raw or not event.get("summary"):
            logger.info(f"sync-holidays skipped (missing summary or start): {event.get('summary')!r}")
            continue
        date_val = start_raw[:10]
        existing = await db["holidays"].find_one({
            "name": event["summary"],
            "year": year,
        })
        if not existing:
            day_name = event.get("day", "")
            if not day_name:
                try:
                    dt = datetime.strptime(date_val, "%Y-%m-%d")
                    day_name = dt.strftime("%A")
                except ValueError:
                    day_name = ""
            now = datetime.now(timezone.utc)
            await db["holidays"].insert_one({
                "name": event["summary"],
                "date": date_val,
                "day": day_name,
                "year": year,
                "description": event.get("description", ""),
                "created_at": now,
                "updated_at": now,
            })
            synced += 1
            logger.info(f"sync-holidays imported: {event['summary']} on {date_val} ({day_name})")
        else:
            logger.info(f"sync-holidays skipped (already exists): {event['summary']}")

    return {"synced": synced, "message": f"Imported {synced} holidays from Google Calendar"}
