from fastapi import APIRouter, HTTPException, status, Depends, Response, Request, Cookie
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
import json
from bson import ObjectId
from datetime import datetime, timezone, timedelta
from models.user import OTPRequest, OTPVerify, WelcomeVerify
from services.auth_service import auth_service
from dependencies import get_current_user, get_current_active_user, rate_limiter, get_client_ip, rollback_rate_limit, reset_rate_limit_for_email
from database import get_database
from config import get_settings


class UpdateMeRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    avatar: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None

    class Config:
        extra = "forbid"


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=8)
    new_password: str = Field(min_length=8)

    class Config:
        extra = "forbid"

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str):
    """Set access and refresh tokens as HttpOnly cookies"""
    _settings = get_settings()
    # Access token: short-lived, Lax SameSite for cross-origin compatibility
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=_settings.frontend_url.startswith("https"),
        samesite="lax",
        path="/",
        max_age=_settings.access_token_expire_minutes * 60,
    )
    # Refresh token: longer-lived, Lax SameSite, only for refresh endpoint
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=_settings.frontend_url.startswith("https"),
        samesite="lax",
        path="/api/auth/refresh",
        max_age=_settings.refresh_token_expire_days * 24 * 60 * 60,
    )


def _clear_auth_cookies(response: Response):
    """Clear authentication cookies"""
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/auth/refresh")


@router.post("/request-otp")
async def request_otp(
    request: OTPRequest,
    fastapi_request: Request,
    _rate_limit=Depends(rate_limiter(max_requests=5, window_seconds=60, email_field="email"))
):
    """Request an OTP to be sent to the user's email"""
    ip_address = get_client_ip(fastapi_request)
    
    try:
        result = await auth_service.request_otp(request.email, request.name, ip_address)
    except Exception as e:
        await rollback_rate_limit(fastapi_request)
        raise e

    if not result.get("success", False):
        await rollback_rate_limit(fastapi_request)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS
            if "Too many" in result.get("error", "")
            else status.HTTP_400_BAD_REQUEST,
            detail=result.get("error", "Failed to process request"),
        )

    response_content: dict = {"message": result["message"], "is_new_user": result["is_new_user"]}
    if "dev_otp" in result:
        response_content["dev_otp"] = result["dev_otp"]
    return JSONResponse(status_code=status.HTTP_200_OK, content=response_content)


@router.post("/reset-rate-limit")
async def reset_rate_limit(request: OTPRequest):
    """Developer/Admin utility to reset rate limits for a specific email"""
    database = get_database()
    # 1. Clear service-level request logs (for backward compatibility if anyone checks it)
    await database.otp_requests.delete_many({"email": request.email})
    
    # 2. Clear endpoint-level rate limits in Redis & MongoDB
    await reset_rate_limit_for_email(request.email)
    
    # 3. Reset verification attempts on user document
    await database.users.update_one(
        {"email": request.email},
        {"$set": {"verification_attempts": 0}}
    )
    return {"message": f"Rate limits successfully reset for {request.email}"}


@router.post("/verify-otp")
async def verify_otp(
    request: OTPVerify,
    response: Response,
    _rate_limit=Depends(rate_limiter(max_requests=10, window_seconds=60)),
):
    """Verify OTP and set auth cookies"""
    result = await auth_service.verify_otp(request.email, request.otp)

    if not result.get("success", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=result.get("error", "Invalid OTP. Please try again.")
        )

    # Build JSONResponse so FastAPI serialises it correctly, then attach cookies
    json_response = JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "user": result["user"],
            "token_type": result["token_type"],
        },
    )
    _set_auth_cookies(json_response, result["access_token"], result["refresh_token"])
    return json_response


@router.post("/welcome-verify")
async def verify_welcome(
    request: WelcomeVerify,
    response: Response,
    _rate_limit=Depends(rate_limiter(max_requests=10, window_seconds=60)),
):
    """Verify welcome token and set auth cookies (no OTP needed)"""
    result = await auth_service.verify_welcome_token(request.email, request.welcome_token)

    if not result.get("success", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=result.get("error", "Invalid or expired welcome link")
        )

    # Build JSONResponse so FastAPI serialises it correctly, then attach cookies
    json_response = JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "user": result["user"],
            "token_type": result["token_type"],
        },
    )
    _set_auth_cookies(json_response, result["access_token"], result["refresh_token"])
    return json_response


@router.post("/refresh")
async def refresh_token(
    request: Request,
    response: Response,
    _rate_limit=Depends(rate_limiter(max_requests=20, window_seconds=60)),
):
    """Refresh access token using refresh token from cookie"""
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Refresh token required"
        )

    result = await auth_service.refresh_tokens(refresh_token)

    if not result.get("success", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=result.get("error", "Invalid refresh token")
        )

    # Build JSONResponse so FastAPI serialises it correctly, then attach cookies
    json_response = JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"token_type": result["token_type"]},
    )
    _set_auth_cookies(json_response, result["access_token"], result["refresh_token"])
    return json_response


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    """Get current authenticated user info"""
    return {
        "id": current_user["id"],
        "email": current_user["email"],
        "name": current_user["name"],
        "role": current_user["role"],
        "avatar": current_user.get("avatar"),
        "email_verified": current_user.get("email_verified", False),
        "settings": current_user.get("settings", {}),
        "github_username": current_user.get("github_username") or None,
        "github_email": current_user.get("github_email") or None,
        "github_avatar_url": current_user.get("github_avatar_url") or None,
        "created_at": current_user.get("created_at"),
        "last_login": current_user.get("last_login"),
        "last_seen": current_user.get("last_seen"),
    }



@router.post("/heartbeat")
async def heartbeat(current_user: dict = Depends(get_current_user)):
    """Update user's last_seen to indicate they are online"""
    db = get_database()
    await db.users.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$set": {"last_seen": datetime.now(timezone.utc)}},
    )
    return {"status": "ok"}


@router.get("/online-users")
async def get_online_users(current_user: dict = Depends(get_current_user)):
    """Get list of users who were active in the last 5 minutes"""
    from datetime import timedelta
    db = get_database()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    cursor = db.users.find(
        {"last_seen": {"$gte": cutoff}},
        {"_id": 1, "name": 1, "email": 1, "role": 1, "avatar": 1, "last_seen": 1},
    )
    users = []
    async for doc in cursor:
        users.append({
            "id": str(doc["_id"]),
            "name": doc.get("name"),
            "email": doc.get("email"),
            "role": doc.get("role"),
            "avatar": doc.get("avatar"),
            "last_seen": doc.get("last_seen"),
        })
    return users


@router.post("/logout")
async def logout(response: Response, current_user: dict = Depends(get_current_user)):
    """Logout - clear auth cookies and invalidate stored refresh tokens"""
    db = get_database()
    await db.users.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$set": {"refresh_token_hashes": []}},
    )
    _clear_auth_cookies(response)
    json_response = JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"message": "Logged out successfully"},
    )
    _clear_auth_cookies(json_response)
    return json_response


@router.put("/me")
async def update_me(
    data: UpdateMeRequest,
    current_user: dict = Depends(get_current_active_user),
):
    """Update current user profile and settings"""
    db = get_database()
    existing = await db.users.find_one({"_id": ObjectId(current_user["id"])})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    update_fields = {}
    if data.name is not None:
        update_fields["name"] = data.name
    if data.avatar is not None:
        update_fields["avatar"] = data.avatar
    if data.settings is not None:
        current_settings = existing.get("settings", {})
        update_fields["settings"] = {**current_settings, **data.settings}

    if update_fields:
        update_fields["updated_at"] = datetime.now(timezone.utc)
        await db.users.update_one({"_id": ObjectId(current_user["id"])}, {"$set": update_fields})

    return {"message": "User profile updated successfully"}


@router.put("/me/password")
async def change_password(
    password_data: ChangePasswordRequest,
    current_user: dict = Depends(get_current_active_user),
    _rate_limit=Depends(rate_limiter(max_requests=5, window_seconds=60)),
):
    """Change user password"""
    from services.auth_service import auth_service

    result = await auth_service.change_password(
        current_user["id"], password_data.current_password, password_data.new_password
    )

    if not result.get("success", False):
        raise HTTPException(status_code=400, detail=result.get("error", "Password change failed"))

    return {"message": "Password changed successfully"}
