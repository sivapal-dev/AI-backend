import logging
import secrets
import urllib.parse
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from datetime import datetime, timezone, timedelta
import httpx
from bson import ObjectId

from config import get_settings
from database import get_database
from dependencies import get_current_active_user, validate_object_id
from utils.encryption import encrypt_password, decrypt_password

router = APIRouter(prefix="/github", tags=["github"])
logger = logging.getLogger(__name__)

@router.get("/connect")
async def github_connect(
    request: Request,
    prompt: Optional[str] = None,
    current_user: dict = Depends(get_current_active_user)
):
    """Redirect to GitHub OAuth page"""
    settings = get_settings()
    client_id = settings.github_client_id
    if not client_id:
        raise HTTPException(status_code=500, detail="GitHub Client ID not configured")
        
    frontend_url = settings.frontend_url
    
    # We'll use the backend URL for the callback so we can set the cookie or process it directly
    backend_url = settings.backend_url
    callback_url = f"{backend_url}/api/github/callback"
    
    # Generate random state to prevent CSRF
    state = secrets.token_urlsafe(32)
    # Store state in DB for verification during callback
    db = get_database()
    await db.oauth_states.update_one(
        {"_id": f"github_{state}"},
        {"$set": {
            "user_id": str(current_user["id"]),
            "created_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
        }},
        upsert=True,
    )
    
    params = {
        "client_id": client_id,
        "redirect_uri": callback_url,
        "scope": "repo",
        "state": state
    }
    if prompt:
        params["prompt"] = prompt
    
    url = f"https://github.com/login/oauth/authorize?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url)


@router.get("/callback")
async def github_callback(
    state: Optional[str] = None,
    code: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    error_uri: Optional[str] = None,
):
    """Handle GitHub OAuth callback"""
    settings = get_settings()
    client_id = settings.github_client_id
    client_secret = settings.github_client_secret
    frontend_url = settings.frontend_url
    
    if not state:
        logger.error("GitHub OAuth callback missing state parameter")
        return RedirectResponse(f"{frontend_url}/dashboard/settings?github_error=invalid_state")

    if error or not code:
        logger.error(f"GitHub OAuth callback error: {error} - {error_description}")
        if state:
            db = get_database()
            await db.oauth_states.delete_one({"_id": f"github_{state}"})
        return RedirectResponse(f"{frontend_url}/dashboard/settings?github_error=auth_failed")

    if not client_id or not client_secret:
        return RedirectResponse(f"{frontend_url}/dashboard/settings?github_error=not_configured")

    db = get_database()
    state_doc = await db.oauth_states.find_one({
        "_id": f"github_{state}",
        "expires_at": {"$gt": datetime.now(timezone.utc)},
    })
    if not state_doc:
        return RedirectResponse(f"{frontend_url}/dashboard/settings?github_error=invalid_state")
    user_id = state_doc["user_id"]
    # Clean up used state
    await db.oauth_states.delete_one({"_id": f"github_{state}"})
    
    # Exchange code for access token
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code
            }
        )
        
        data = response.json()
        access_token = data.get("access_token")
        
        if not access_token:
            logger.error(f"GitHub OAuth failed: {data}")
            return RedirectResponse(f"{frontend_url}/dashboard/settings?github_error=auth_failed")

        # Get user profile from GitHub
        user_response = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        user_data = user_response.json()
        github_username = user_data.get("login")

    if not github_username:
        return RedirectResponse(f"{frontend_url}/dashboard/settings?github_error=profile_failed")

        # Save to database — encrypt the access token before storing
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "github_access_token": encrypt_password(access_token),
                "github_username": github_username,
                "github_email": user_data.get("email"),
                "github_avatar_url": user_data.get("avatar_url"),
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )

    return RedirectResponse(f"{frontend_url}/dashboard/settings?github_success=1")


@router.post("/disconnect")
async def github_disconnect(current_user: dict = Depends(get_current_active_user)):
    """Disconnect GitHub account"""
    db = get_database()
    await db.users.update_one(
        {"_id": validate_object_id(current_user["id"])},
        {
            "$unset": {
                "github_access_token": "",
                "github_username": "",
                "github_email": "",
                "github_avatar_url": ""
            },
            "$set": {
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )
    return {"success": True}
