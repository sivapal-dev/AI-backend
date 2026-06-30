from fastapi import APIRouter, HTTPException, Depends, status, Request
from fastapi.responses import JSONResponse
from bson import ObjectId
from datetime import datetime, timezone
from typing import Optional, List
import logging

from dependencies import get_current_active_user
from database import get_database
from models.user import EmailCredentials
from services.imap_service import (
    test_connection,
    fetch_emails,
    fetch_email_body,
    list_folders,
)
from utils.encryption import encrypt_password, decrypt_password
from redis_client import get_redis, cache_get, cache_set, cache_delete_pattern
from config import get_settings
import json

router = APIRouter(prefix="/inbox", tags=["Inbox"])
logger = logging.getLogger(__name__)


def _get_db():
    return get_database()


def _normalize_creds(creds) -> Optional[dict]:
    if not creds:
        return None
    if hasattr(creds, "model_dump"):
        return creds.model_dump()
    if isinstance(creds, dict):
        return creds
    try:
        return dict(creds)
    except Exception:
        return None


def _sanitize_msg(msg: str, password: str) -> str:
    if password and password in msg:
        return msg.replace(password, "********")
    return msg


def _mask_email(email: str) -> str:
    """Mask email for display: john@by8labs.com → jo**@by8labs.com"""
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[:2] + "*" * (len(local) - 2)
    return f"{masked_local}@{domain}"


# ─── Cache key helpers ───────────────────────────────────────────────────────

def _status_key(user_id: str) -> str:
    return f"inbox:status:{user_id}"


def _emails_key(user_id: str, folder: str, limit: int, offset: int) -> str:
    return f"inbox:emails:{user_id}:{folder}:limit={limit}:offset={offset}"


def _body_key(user_id: str, message_id: str) -> str:
    return f"inbox:body:{user_id}:{message_id}"


def _folders_key(user_id: str) -> str:
    return f"inbox:folders:{user_id}"


from pydantic import BaseModel

class InboxConnectRequest(BaseModel):
    email_address: str
    password: str

# ─── Connect / Disconnect — invalidate user cache ────────────────────────────

@router.post("/connect")
async def connect_inbox(
    payload: InboxConnectRequest,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Connect user's company email to inbox.

    Request body: { "email_address": "john@by8labs.com", "password": "plaintext" }
    """
    email_address = payload.email_address
    password = payload.password

    if not email_address or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    db = _get_db()

    # Determine imap_host and imap_port based on the email address domain
    domain = email_address.split("@")[-1].lower() if "@" in email_address else ""
    imap_host = "imap.hostinger.com"  # fallback default
    imap_port = 993

    if domain == "gmail.com":
        imap_host = "imap.gmail.com"
    elif domain in ("outlook.com", "hotmail.com", "office365.com"):
        imap_host = "outlook.office365.com"
    elif domain == "yahoo.com":
        imap_host = "imap.mail.yahoo.com"

    # Test IMAP connection before storing
    try:
        connection_ok = await test_connection(email_address, password, imap_host, imap_port)
    except Exception as e:
        sanitized_err = _sanitize_msg(str(e), password)
        logger.error(f"Failed to connect to email server: {sanitized_err}")
        raise HTTPException(
            status_code=400,
            detail=f"Failed to connect to email server: {sanitized_err}",
        ) from None
    if not connection_ok:
        detail_msg = "Failed to connect to email server. Please check your email and password."
        if domain == "gmail.com":
            detail_msg += " For Gmail accounts, please ensure you use a 16-character Google 'App Password' instead of your main account password, and that IMAP is enabled in your Gmail settings."
        raise HTTPException(
            status_code=400,
            detail=detail_msg,
        )

    # Encrypt password
    try:
        encrypted_password = encrypt_password(password)
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to secure credentials")

    # Store encrypted credentials
    email_creds = EmailCredentials(
        email_address=email_address,
        encrypted_password=encrypted_password,
        imap_host=imap_host,
        imap_port=imap_port,
        is_connected=True,
    )

    await db.users.update_one(
        {"_id": ObjectId(current_user["id"])},
        {
            "$set": {
                "email_credentials": email_creds.model_dump(),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    # Invalidate all inbox cache for this user
    user_id = current_user["id"]
    pattern = f"inbox:*:{user_id}*"
    await cache_delete_pattern(pattern)

    return {"success": True, "message": "Email connected successfully", "email": email_address}


@router.post("/disconnect")
async def disconnect_inbox(
    current_user: dict = Depends(get_current_active_user),
):
    """Disconnect email integration (remove stored credentials)"""
    db = _get_db()

    await db.users.update_one(
        {"_id": ObjectId(current_user["id"])},
        {
            "$unset": {"email_credentials": ""},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

    # Invalidate all inbox cache for this user
    user_id = current_user["id"]
    pattern = f"inbox:*:{user_id}*"
    await cache_delete_pattern(pattern)

    return {"success": True, "message": "Email disconnected"}


# ─── Status ───────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_inbox_status(
    current_user: dict = Depends(get_current_active_user),
):
    """Get inbox connection status and masked email"""
    settings = get_settings()
    user_id = current_user["id"]
    key = _status_key(user_id)

    # Try cache first
    cached = await cache_get(key)
    if cached is not None:
        return cached

    db = _get_db()
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    creds = _normalize_creds(user.get("email_credentials"))
    result = (
        {
            "is_connected": True,
            "email_address": _mask_email(creds["email_address"]),
            "imap_host": creds.get("imap_host"),
        }
        if creds
        else {"is_connected": False, "email_address": None}
    )

    # Cache for 5 minutes
    await cache_set(key, result, settings.inbox_cache_status_ttl)
    return result


# ─── Emails list ─────────────────────────────────────────────────────────────

@router.get("/emails")
async def get_emails(
    folder: str = "INBOX",
    limit: int = 20,
    offset: int = 0,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Fetch emails from user's connected inbox.

    Args:
        folder: Mailbox folder name (default: INBOX)
        limit: Number of emails to fetch (max 50)
        offset: Number of emails to skip (for pagination)
    """
    if offset < 0:
        raise HTTPException(status_code=400, detail="Offset must be a non-negative integer")
    limit = min(limit, 50)
    settings = get_settings()
    user_id = current_user["id"]
    key = _emails_key(user_id, folder, limit, offset)

    # Try cache first
    cached = await cache_get(key)
    if cached is not None:
        return cached

    db = _get_db()
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    creds = _normalize_creds(user.get("email_credentials"))
    if not creds:
        raise HTTPException(
            status_code=400, detail="No email connected. Connect your email first."
        )

    # Decrypt password
    try:
        password = decrypt_password(creds["encrypted_password"])
    except Exception as e:
        logger.error(f"Failed to decrypt password for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to decrypt email credentials")

    email_address = creds["email_address"]

    try:
        emails = await fetch_emails(
            email_address,
            password,
            folder=folder,
            limit=limit,
            offset=offset,
            imap_host=creds.get("imap_host"),
            imap_port=creds.get("imap_port"),
        )
        result = {"emails": emails, "total": len(emails)}
        # Cache for 2 minutes
        await cache_set(key, result, settings.inbox_cache_emails_ttl)
        return result
    except ValueError as e:
        sanitized_err = _sanitize_msg(str(e), password)
        raise HTTPException(status_code=400, detail=sanitized_err) from None
    except Exception as e:
        sanitized_err = _sanitize_msg(str(e), password)
        logger.error(f"Failed to fetch emails: {sanitized_err}")
        raise HTTPException(status_code=500, detail="Failed to fetch emails") from None


# ─── Single email body ────────────────────────────────────────────────────────

@router.get("/email/{message_id}")
async def get_email(
    message_id: str,
    folder: str = "INBOX",
    current_user: dict = Depends(get_current_active_user),
):
    """Fetch full email content (body + attachments) by Message-ID"""
    user_id = current_user["id"]
    key = _body_key(user_id, message_id)

    # Try cache first
    cached = await cache_get(key)
    if cached is not None:
        return cached

    db = _get_db()
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    creds = _normalize_creds(user.get("email_credentials"))
    if not creds:
        raise HTTPException(
            status_code=400, detail="No email connected. Connect your email first."
        )

    try:
        password = decrypt_password(creds["encrypted_password"])
    except Exception as e:
        logger.error(f"Failed to decrypt password: {e}")
        raise HTTPException(status_code=500, detail="Failed to decrypt credentials")

    email_address = creds["email_address"]

    try:
        email_data = await fetch_email_body(
            email_address,
            password,
            message_id,
            folder=folder,
            imap_host=creds.get("imap_host"),
            imap_port=creds.get("imap_port"),
        )
        if email_data is None:
            raise HTTPException(status_code=404, detail="Email not found")

        # Cache body for 30 minutes
        settings = get_settings()
        await cache_set(key, email_data, settings.inbox_cache_body_ttl)
        return email_data
    except ValueError as e:
        sanitized_err = _sanitize_msg(str(e), password)
        raise HTTPException(status_code=400, detail=sanitized_err) from None
    except Exception as e:
        sanitized_err = _sanitize_msg(str(e), password)
        logger.error(f"Failed to fetch email body: {sanitized_err}")
        raise HTTPException(status_code=500, detail="Failed to fetch email") from None


# ─── Folders list ─────────────────────────────────────────────────────────────

@router.get("/folders")
async def get_folders(
    current_user: dict = Depends(get_current_active_user),
):
    """List available mailbox folders"""
    user_id = current_user["id"]
    key = _folders_key(user_id)

    # Try cache first
    cached = await cache_get(key)
    if cached is not None:
        return {"folders": cached}

    db = _get_db()
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    creds = _normalize_creds(user.get("email_credentials"))
    if not creds:
        raise HTTPException(
            status_code=400, detail="No email connected. Connect your email first."
        )

    try:
        password = decrypt_password(creds["encrypted_password"])
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to decrypt credentials")

    email_address = creds["email_address"]

    try:
        folders = await list_folders(
            email_address,
            password,
            imap_host=creds.get("imap_host"),
            imap_port=creds.get("imap_port"),
        )
        # Cache for 10 minutes
        settings = get_settings()
        await cache_set(key, folders, settings.inbox_cache_folders_ttl)
        return {"folders": folders}
    except Exception as e:
        sanitized_err = _sanitize_msg(str(e), password)
        logger.error(f"Failed to list folders: {sanitized_err}")
        raise HTTPException(status_code=500, detail="Failed to list folders") from None
