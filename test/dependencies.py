import time
from collections import defaultdict
from fastapi import Depends, HTTPException, status, Request
from services.auth_service import auth_service
from utils.security import decode_token
from datetime import datetime, timezone
from bson import ObjectId
from bson.errors import InvalidId
import logging

logger = logging.getLogger(__name__)

# In-memory sliding window rate limiter fallback store
FALLBACK_STORE = defaultdict(list)
FALLBACK_CLEANUP_INTERVAL = 300  # clean up every 5 minutes
last_fallback_cleanup = time.time()


def check_in_memory_rate_limit(key: str, max_requests: int, window_seconds: int) -> bool:
    """
    In-memory rate limiter fallback using sliding window.
    Returns True if request is allowed, False if rate limited.
    """
    global last_fallback_cleanup
    now = time.time()

    # Periodic cleanup of old keys to prevent memory leaks
    if now - last_fallback_cleanup > FALLBACK_CLEANUP_INTERVAL:
        for k in list(FALLBACK_STORE.keys()):
            FALLBACK_STORE[k] = [t for t in FALLBACK_STORE[k] if now - t < window_seconds]
            if not FALLBACK_STORE[k]:
                del FALLBACK_STORE[k]
        last_fallback_cleanup = now

    # Filter timestamps within current window
    timestamps = FALLBACK_STORE[key]
    active_timestamps = [t for t in timestamps if now - t < window_seconds]

    if len(active_timestamps) >= max_requests:
        FALLBACK_STORE[key] = active_timestamps
        return False

    active_timestamps.append(now)
    FALLBACK_STORE[key] = active_timestamps
    return True


def get_client_ip(request: Request) -> str:
    """Extract real client IP from request headers if behind a reverse proxy."""
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    x_real_ip = request.headers.get("x-real-ip")
    if x_real_ip:
        return x_real_ip.strip()
    if request.client:
        return request.client.host
    return "unknown"


def rate_limiter(max_requests: int = 10, window_seconds: int = 60):
    """Returns a dependency that rate-limits by client IP using Redis."""
    from redis_client import get_redis

    async def _check_rate_limit(request: Request):
        client_ip = get_client_ip(request)
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            return
        endpoint = request.url.path
        key = f"ratelimit:{endpoint}:{client_ip}"
        try:
            redis_client = get_redis()
            if redis_client is None:
                raise Exception("Redis not available")
            count = await redis_client.incr(key)
            ttl = await redis_client.ttl(key)
            if ttl == -1:
                await redis_client.expire(key, window_seconds)

            if count > max_requests:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded. Please try again later.",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Rate limiter Redis failure: {e}. Falling back to in-memory rate limiter.")
            try:
                allowed = check_in_memory_rate_limit(key, max_requests, window_seconds)
                if not allowed:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Rate limit exceeded. Please try again later.",
                    )
            except HTTPException:
                raise
            except Exception as fallback_err:
                logger.error(f"In-memory rate limiter fallback failed: {fallback_err}. Bypassing as last resort.")


def validate_object_id(id_str: str, field_name: str = "id") -> ObjectId:
    try:
        return ObjectId(id_str)
    except InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name} format",
        )


async def get_current_user(request: Request):
    """Get current user from access_token cookie (HttpOnly)"""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    from jose import jwt
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        logger.warning("[Auth] Token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.JWTError:
        logger.warning("[Auth] Token invalid")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_type = payload.get("type")
    if token_type != "access":
        logger.warning(f"[Auth] Wrong token type: {token_type}, expected access")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    logger.debug(f"[Auth] Token valid, user_id: {user_id}")
    user = await auth_service.get_user_by_id(user_id)

    if not user:
        logger.warning(f"[Auth] User not found: {user_id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_current_active_user(current_user: dict = Depends(get_current_user)):
    if not current_user.get("email_verified", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Email not verified"
        )
    return current_user


def require_admin(current_user: dict = Depends(get_current_active_user)):
    if current_user.get("role", "").lower() != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user


def require_admin_or_hr(current_user: dict = Depends(get_current_active_user)):
    role = current_user.get("role", "").lower()
    if role not in ("admin", "hr"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or HR access required"
        )
    return current_user

