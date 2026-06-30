"""Microsoft Graph API service for Teams integration."""
import asyncio
import json
import logging
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import requests

from database import get_database
from config import get_settings
from utils.encryption import encrypt_password, decrypt_password

logger = logging.getLogger(__name__)

TEAMS_AUTHORITY = "https://login.microsoftonline.com"
GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0"


class MicrosoftLicenseError(Exception):
    """Exception raised when Microsoft Graph API lacks necessary license."""
    pass

# Cache TTLs in seconds
CACHE_TTL_CHATS = 1800
CACHE_TTL_MESSAGES = 600
CACHE_TTL_TEAMS = 3600
CACHE_TTL_CHANNELS = 1800
CACHE_TTL_MEETINGS = 900
CACHE_TTL_TRANSCRIPTS = 3600


def _now():
    return datetime.now(timezone.utc)


def _cache_key(prefix: str, *parts: str) -> str:
    return f"{prefix}:{':'.join(parts)}"


class MicrosoftGraphService:
    def __init__(self):
        settings = get_settings()
        self.client_id = settings.msal_client_id
        self.tenant_id = settings.msal_tenant_id
        self.client_secret = settings.msal_client_secret
        self.authority = f"{TEAMS_AUTHORITY}/{self.tenant_id}"

    async def _get_tokens(self, user_id: str) -> Optional[Dict[str, Any]]:
        db = get_database()
        doc = await db["oauth_tokens"].find_one({
            "user_id": user_id,
            "provider": "microsoft",
        })
        if doc and doc.get("access_token"):
            try:
                if "." in doc["access_token"]:
                    doc["access_token"] = decrypt_password(doc["access_token"])
                if doc.get("refresh_token") and "." in doc["refresh_token"]:
                    doc["refresh_token"] = decrypt_password(doc["refresh_token"])
            except Exception as e:
                logger.error(f"Failed to decrypt MSAL tokens for user {user_id}: {e}")
                return None

            expires_at = doc.get("expires_at")
            if expires_at:
                if isinstance(expires_at, str):
                    try:
                        expires_at = datetime.fromisoformat(expires_at)
                    except ValueError:
                        expires_at = None
                if expires_at and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(timezone.utc)
            if not expires_at or expires_at <= _now():
                logger.info(f"MSAL token expired or invalid for user {user_id}, attempting refresh")
                return await self._refresh_token(doc, user_id)
            return doc
        return None

    async def _refresh_token(self, token_doc: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
        refresh_token = token_doc.get("refresh_token")
        if not refresh_token:
            logger.warning(f"No refresh_token found for user {user_id}")
            return None
        if "." in refresh_token:
            try:
                refresh_token = decrypt_password(refresh_token)
            except Exception as e:
                logger.error(f"Failed to decrypt MSAL refresh token for user {user_id} in refresh: {e}")
                return None

        try:
            resp = await asyncio.to_thread(
                requests.post,
                f"{self.authority}/oauth2/v2.0/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                    "scope": " ".join([
                        "User.Read", "Chat.Read", "ChannelMessage.Read.All",
                        "Team.ReadBasic.All", "OnlineMeetings.Read",
                        "OnlineMeetingTranscript.Read.All", "offline_access",
                    ]),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            if resp.status_code != 200:
                logger.error(f"Token refresh failed: {resp.text}")
                return None
            tokens = resp.json()
            expires_in = tokens.get("expires_in", 3600)
            db = get_database()
            enc_access_token = encrypt_password(tokens["access_token"])
            enc_refresh_token = encrypt_password(tokens.get("refresh_token", refresh_token))
            await db["oauth_tokens"].update_one(
                {"user_id": user_id, "provider": "microsoft"},
                {"$set": {
                    "access_token": enc_access_token,
                    "refresh_token": enc_refresh_token,
                    "expires_at": _now() + timedelta(seconds=expires_in),
                    "updated_at": _now(),
                }},
            )
            token_doc["access_token"] = tokens["access_token"]
            token_doc["refresh_token"] = tokens.get("refresh_token", refresh_token)
            return token_doc
        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            return None

    async def _get_cached(self, cache_key: str) -> Optional[Dict[str, Any]]:
        db = get_database()
        doc = await db["teams_cache"].find_one({"_id": cache_key})
        if doc and doc.get("expires_at"):
            exp = doc["expires_at"]
            if isinstance(exp, str):
                try:
                    exp = datetime.fromisoformat(exp)
                except ValueError:
                    return None
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp > _now():
                return doc.get("data")
        return None

    async def _set_cache(self, cache_key: str, data: Any, ttl: int):
        db = get_database()
        await db["teams_cache"].update_one(
            {"_id": cache_key},
            {"$set": {
                "data": data,
                "cached_at": _now(),
                "expires_at": _now() + timedelta(seconds=ttl),
            }},
            upsert=True,
        )

    async def _graph_get(self, user_id: str, path: str, cache_ttl: int = 0) -> Optional[Any]:
        tokens = await self._get_tokens(user_id)
        if not tokens:
            return None
        access_token = tokens.get("access_token")
        if not access_token:
            return None
        ck = _cache_key("graph", user_id, path)
        if cache_ttl > 0:
            cached = await self._get_cached(ck)
            if cached is not None:
                return cached
        url = f"{GRAPH_ENDPOINT}{path}"
        try:
            resp = await asyncio.to_thread(
                requests.get,
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                timeout=30,
            )
            if resp.status_code == 401:
                tokens = await self._refresh_token(tokens, user_id)
                if not tokens:
                    return None
                access_token = tokens["access_token"]
                resp = await asyncio.to_thread(
                    requests.get,
                    url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                    timeout=30,
                )
            if resp.status_code != 200:
                logger.error(f"Graph API error {resp.status_code}: {path} -> {resp.text[:300]}")
                if resp.status_code == 403:
                    try:
                        err = resp.json().get("error", {})
                        msg = err.get("message", "Forbidden")
                        msg_lower = msg.lower()
                        is_license_err = (
                            "license required" in msg_lower
                            or "not licensed" in msg_lower
                            or "no license" in msg_lower
                            or "missing license" in msg_lower
                            or ("license" in msg_lower and "office" in msg_lower)
                            or ("office 365" in msg_lower and "license" in msg_lower)
                            or ("license" in msg_lower and "microsoft" in msg_lower)
                        )
                        if is_license_err:
                            logger.error(f"Office 365 license required for user {user_id}")
                            raise MicrosoftLicenseError(f"Office 365 license required: {msg}")
                    except MicrosoftLicenseError:
                        raise
                    except Exception:
                        pass
                return None
            data = resp.json()
            if cache_ttl > 0:
                await self._set_cache(ck, data, cache_ttl)
            return data
        except MicrosoftLicenseError:
            raise
        except Exception as e:
            logger.error(f"Graph GET error: {path} -> {e}")
            return None

    async def get_chats(self, user_id: str) -> Optional[List[Dict[str, Any]]]:
        data = await self._graph_get(user_id, "/me/chats?$expand=members&$top=50", CACHE_TTL_CHATS)
        if data:
            return data.get("value", [])
        return None

    async def get_chat_messages(self, user_id: str, chat_id: str) -> Optional[List[Dict[str, Any]]]:
        data = await self._graph_get(
            user_id,
            f"/me/chats/{chat_id}/messages?$top=50&$orderBy=createdDateTime desc",
            CACHE_TTL_MESSAGES,
        )
        if data:
            msgs = data.get("value", [])
            msgs.reverse()
            return msgs
        return None

    async def get_joined_teams(self, user_id: str) -> Optional[List[Dict[str, Any]]]:
        data = await self._graph_get(user_id, "/me/joinedTeams", CACHE_TTL_TEAMS)
        if data:
            return data.get("value", [])
        return None

    async def get_team_channels(self, user_id: str, team_id: str) -> Optional[List[Dict[str, Any]]]:
        data = await self._graph_get(
            user_id,
            f"/teams/{team_id}/channels",
            CACHE_TTL_CHANNELS,
        )
        if data:
            return data.get("value", [])
        return None

    async def get_channel_messages(self, user_id: str, team_id: str, channel_id: str) -> Optional[List[Dict[str, Any]]]:
        data = await self._graph_get(
            user_id,
            f"/teams/{team_id}/channels/{channel_id}/messages?$top=50&$orderBy=createdDateTime desc",
            CACHE_TTL_MESSAGES,
        )
        if data:
            msgs = data.get("value", [])
            msgs.reverse()
            return msgs
        return None

    async def get_online_meetings(self, user_id: str) -> Optional[List[Dict[str, Any]]]:
        data = await self._graph_get(user_id, "/me/onlineMeetings?$top=50&$orderBy=startDateTime desc", CACHE_TTL_MEETINGS)
        if data:
            return data.get("value", [])
        return None

    async def get_meeting_transcripts(self, user_id: str, meeting_id: str) -> Optional[List[Dict[str, Any]]]:
        data = await self._graph_get(
            user_id,
            f"/me/onlineMeetings/{meeting_id}/transcripts",
            CACHE_TTL_TRANSCRIPTS,
        )
        if data:
            return data.get("value", [])
        return None

    async def get_meeting_transcript_content(self, user_id: str, meeting_id: str, transcript_id: str) -> Optional[str]:
        data = await self._graph_get(
            user_id,
            f"/me/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content?$format=text/vtt",
            CACHE_TTL_TRANSCRIPTS,
        )
        if data and isinstance(data, dict):
            return data.get("content")
        return data

    async def get_status(self, user_id: str) -> dict:
        tokens = await self._get_tokens(user_id)
        if not tokens:
            return {"connected": False}
        return {
            "connected": True,
            "email": tokens.get("email", ""),
        }

    async def disconnect(self, user_id: str):
        db = get_database()
        await db["oauth_tokens"].delete_one({
            "user_id": user_id,
            "provider": "microsoft",
        })
        await db["teams_cache"].delete_many({"_id": {"$regex": f"^graph:{user_id}:"}})


microsoft_graph_service = MicrosoftGraphService()
