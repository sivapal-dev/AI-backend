"""Microsoft Teams OAuth2 + Graph API endpoints (read-only)."""
import asyncio
import secrets
import urllib.parse
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from dependencies import get_current_user
from database import get_database
from services.microsoft_graph_service import microsoft_graph_service
from config import get_settings
from utils.encryption import encrypt_password, decrypt_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/teams", tags=["teams"])

settings = get_settings()


def _check_license(data):
    if isinstance(data, dict) and data.get("_error") == "license_required":
        raise HTTPException(status_code=403, detail=data.get("_message", "Office 365 license required to access Teams data"))


class ConnectResponse(BaseModel):
    url: str
    state: str


class StatusResponse(BaseModel):
    connected: bool
    email: Optional[str] = None


class ChatPreview(BaseModel):
    id: str
    topic: Optional[str] = None
    chat_type: str
    last_message_preview: Optional[str] = None
    last_message_time: Optional[str] = None
    members: List[dict] = []


class MessageItem(BaseModel):
    id: str
    message_type: str
    content: Optional[str] = None
    sender_name: Optional[str] = None
    sender_id: Optional[str] = None
    created_datetime: Optional[str] = None


class TeamItem(BaseModel):
    id: str
    display_name: str
    description: Optional[str] = None


class ChannelItem(BaseModel):
    id: str
    display_name: str
    description: Optional[str] = None


class MeetingItem(BaseModel):
    id: str
    subject: Optional[str] = None
    start_datetime: Optional[str] = None
    end_datetime: Optional[str] = None
    join_url: Optional[str] = None
    participants: List[dict] = []


class TranscriptItem(BaseModel):
    id: str
    created_datetime: Optional[str] = None


@router.get("/connect")
async def connect_teams(
    current_user: dict = Depends(get_current_user),
):
    user_id = str(current_user["id"])
    state = secrets.token_urlsafe(32)

    db = get_database()
    await db["oauth_states"].insert_one({
        "_id": state,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
    })

    redirect_uri = f"{settings.frontend_url}/api/teams/callback"

    scopes = [
        "User.Read",
        "Chat.Read",
        "ChannelMessage.Read.All",
        "Channel.ReadBasic.All",
        "Team.ReadBasic.All",
        "OnlineMeetings.Read",
        "OnlineMeetingTranscript.Read.All",
        "offline_access",
    ]

    params = {
        "client_id": settings.msal_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "response_mode": "query",
        "scope": " ".join(scopes),
        "state": state,
    }

    authority = f"https://login.microsoftonline.com/{settings.msal_tenant_id}"
    auth_url = f"{authority}/oauth2/v2.0/authorize?{urllib.parse.urlencode(params)}"

    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def teams_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    db = get_database()
    state_doc = await db["oauth_states"].find_one({
        "_id": state,
        "expires_at": {"$gt": datetime.now(timezone.utc)},
    })
    if not state_doc:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    user_id = state_doc.get("user_id", "")

    await db["oauth_states"].delete_one({"_id": state})
    redirect_uri = f"{settings.frontend_url}/api/teams/callback"

    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            f"https://login.microsoftonline.com/{settings.msal_tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": settings.msal_client_id,
                "client_secret": settings.msal_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "scope": "User.Read Chat.Read ChannelMessage.Read.All Channel.ReadBasic.All Team.ReadBasic.All OnlineMeetings.Read OnlineMeetingTranscript.Read.All offline_access",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30.0,
        )

    if token_response.status_code != 200:
        err_body = token_response.text[:500]
        logger.error(f"MSAL token exchange failed (HTTP {token_response.status_code}): {err_body}")
        try:
            err_data = token_response.json()
            msal_error = err_data.get("error", "unknown")
        except Exception:
            msal_error = f"http_{token_response.status_code}"
        logger.error(f"MSAL error code: {msal_error}, body: {err_body}")
        return RedirectResponse(
            url=f"{settings.frontend_url}/dashboard/settings?teams=error&reason={urllib.parse.quote(msal_error)}"
        )

    tokens = token_response.json()
    expires_in = tokens.get("expires_in", 3600)

    email = ""
    try:
        async with httpx.AsyncClient() as client:
            me_resp = await client.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
                timeout=10.0,
            )
        if me_resp.status_code == 200:
            email = me_resp.json().get("mail") or me_resp.json().get("userPrincipalName", "")
    except Exception as e:
        logger.warning(f"Could not fetch user email after MSAL auth: {e}")

    await db["oauth_tokens"].update_one(
        {"user_id": user_id, "provider": "microsoft"},
        {"$set": {
            "access_token": encrypt_password(tokens["access_token"]),
            "refresh_token": encrypt_password(tokens.get("refresh_token", "")) if tokens.get("refresh_token") else "",
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            "email": email,
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )

    return RedirectResponse(
        url=f"{settings.frontend_url}/dashboard/settings?teams=connected"
    )


@router.get("/status", response_model=StatusResponse)
async def get_teams_status(
    current_user: dict = Depends(get_current_user),
):
    return await microsoft_graph_service.get_status(str(current_user["id"]))


@router.post("/disconnect")
async def disconnect_teams(
    current_user: dict = Depends(get_current_user),
):
    await microsoft_graph_service.disconnect(str(current_user["id"]))
    return {"message": "Teams disconnected successfully"}


@router.get("/chats")
async def list_chats(
    current_user: dict = Depends(get_current_user),
):
    chats = await microsoft_graph_service.get_chats(str(current_user["id"]))
    _check_license(chats)
    if chats is None:
        raise HTTPException(status_code=400, detail="Failed to fetch chats. Is Teams connected?")
    result = []
    for c in chats:
        members = c.get("members", [])
        result.append({
            "id": c.get("id"),
            "topic": c.get("topic"),
            "chat_type": c.get("chatType", "unknown"),
            "last_message_preview": (
                c.get("lastMessagePreview", {}).get("content", {}).get("text", "")
                if c.get("lastMessagePreview") else ""
            ),
            "last_message_time": c.get("lastMessagePreview", {}).get("createdDateTime") if c.get("lastMessagePreview") else None,
            "members": [
                {"id": m.get("userId"), "display_name": m.get("displayName"), "email": m.get("email")}
                for m in members if m.get("userId") != str(current_user["id"])
            ],
        })
    return result


@router.get("/chats/{chat_id}/messages")
async def get_chat_messages(
    chat_id: str,
    current_user: dict = Depends(get_current_user),
):
    messages = await microsoft_graph_service.get_chat_messages(str(current_user["id"]), chat_id)
    _check_license(messages)
    if messages is None:
        raise HTTPException(status_code=400, detail="Failed to fetch messages")
    result = []
    for m in messages:
        content = m.get("body", {}).get("content", "")
        sender = m.get("from", {})
        sender_user = sender.get("user", {}) if isinstance(sender, dict) else {}
        result.append({
            "id": m.get("id"),
            "message_type": m.get("messageType", "message"),
            "content": content,
            "sender_name": sender_user.get("displayName", "Unknown"),
            "sender_id": sender_user.get("id", ""),
            "created_datetime": m.get("createdDateTime"),
        })
    return result


@router.get("/teams")
async def list_teams(
    current_user: dict = Depends(get_current_user),
):
    teams = await microsoft_graph_service.get_joined_teams(str(current_user["id"]))
    _check_license(teams)
    if teams is None:
        raise HTTPException(status_code=400, detail="Failed to fetch teams")
    return [
        {"id": t.get("id"), "display_name": t.get("displayName", ""), "description": t.get("description", "")}
        for t in teams
    ]


@router.get("/teams/{team_id}/channels")
async def list_channels(
    team_id: str,
    current_user: dict = Depends(get_current_user),
):
    channels = await microsoft_graph_service.get_team_channels(str(current_user["id"]), team_id)
    _check_license(channels)
    if channels is None:
        raise HTTPException(status_code=400, detail="Failed to fetch channels")
    return [
        {"id": ch.get("id"), "display_name": ch.get("displayName", ""), "description": ch.get("description", "")}
        for ch in channels
    ]


@router.get("/channels/{team_id}/{channel_id}/messages")
async def get_channel_messages(
    team_id: str,
    channel_id: str,
    current_user: dict = Depends(get_current_user),
):
    messages = await microsoft_graph_service.get_channel_messages(
        str(current_user["id"]), team_id, channel_id
    )
    _check_license(messages)
    if messages is None:
        raise HTTPException(status_code=400, detail="Failed to fetch channel messages")
    result = []
    for m in messages:
        content = m.get("body", {}).get("content", "")
        sender = m.get("from", {})
        sender_user = sender.get("user", {}) if isinstance(sender, dict) else {}
        result.append({
            "id": m.get("id"),
            "message_type": m.get("messageType", "message"),
            "content": content,
            "sender_name": sender_user.get("displayName", "Unknown"),
            "sender_id": sender_user.get("id", ""),
            "created_datetime": m.get("createdDateTime"),
        })
    return result


@router.get("/meetings")
async def list_meetings(
    current_user: dict = Depends(get_current_user),
):
    meetings = await microsoft_graph_service.get_online_meetings(str(current_user["id"]))
    _check_license(meetings)
    if meetings is None:
        raise HTTPException(status_code=400, detail="Failed to fetch meetings")
    result = []
    for m in meetings:
        participants = m.get("participants", {})
        all_parts = []
        for role in ["organizer", "attendees"]:
            entries = participants.get(role, [])
            if not isinstance(entries, list):
                entries = [entries] if entries else []
            for p in entries:
                info = p.get("identity", {}).get("user", {}) if isinstance(p, dict) else {}
                all_parts.append({
                    "id": info.get("id", ""),
                    "display_name": info.get("displayName", ""),
                })
        result.append({
            "id": m.get("id"),
            "subject": m.get("subject", "Untitled Meeting"),
            "start_datetime": m.get("startDateTime"),
            "end_datetime": m.get("endDateTime"),
            "join_url": m.get("joinUrl"),
            "participants": all_parts,
        })
    return result


@router.get("/meetings/{meeting_id}/transcripts")
async def get_meeting_transcripts(
    meeting_id: str,
    current_user: dict = Depends(get_current_user),
):
    transcripts = await microsoft_graph_service.get_meeting_transcripts(
        str(current_user["id"]), meeting_id
    )
    _check_license(transcripts)
    if transcripts is None:
        raise HTTPException(status_code=400, detail="Failed to fetch transcripts")
    result = []
    for t in transcripts:
        result.append({
            "id": t.get("id"),
            "created_datetime": t.get("createdDateTime"),
        })
    return result


@router.get("/meetings/{meeting_id}/transcripts/{transcript_id}/content")
async def get_transcript_content(
    meeting_id: str,
    transcript_id: str,
    current_user: dict = Depends(get_current_user),
):
    content = await microsoft_graph_service.get_meeting_transcript_content(
        str(current_user["id"]), meeting_id, transcript_id
    )
    _check_license(content)
    if content is None:
        raise HTTPException(status_code=400, detail="Failed to fetch transcript content")
    return {"content": content}
