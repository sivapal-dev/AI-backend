import time
import json
from collections import defaultdict
from fastapi import Depends, HTTPException, status, Request
from services.auth_service import auth_service
from utils.security import decode_token
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from bson.errors import InvalidId
from database import get_database
import logging

logger = logging.getLogger(__name__)

# In-memory sliding window rate limiter fallback store
FALLBACK_STORE = defaultdict(list)
FALLBACK_CLEANUP_INTERVAL = 300  # clean up every 5 minutes
last_fallback_cleanup = time.time()


async def check_mongo_rate_limit(
    key: str,
    max_requests: int,
    window_seconds: int,
    email: str = "",
    client_ip: str = "",
) -> bool:
    """
    MongoDB-based rate limiter — correct sliding window.

    Order of operations (CRITICAL — never change this order):
      1. Delete the whole document if all its timestamps are expired (stale deploy cleanup)
      2. Pull any individual expired timestamps from within the document
      3. READ and COUNT valid timestamps BEFORE recording this request
      4. If count >= max_requests → BLOCK (do NOT write a new timestamp)
      5. If allowed → push the new timestamp

    This guarantees the first request always starts at count=0.
    """
    try:
        database = get_database()
        if database is None:
            logger.warning(
                f"[RateLimit/Mongo] DB unavailable | key={key} email={email} ip={client_ip}. "
                "Passing to in-memory fallback."
            )
            raise Exception("MongoDB unavailable")

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=window_seconds)

        # Step 1: Delete entire stale document if ALL timestamps are expired
        #         This removes pollution from old deploys, health checks, or previous test runs.
        await database.rate_limits.delete_one(
            {
                "_id": key,
                "timestamps": {"$not": {"$gt": cutoff}},  # no timestamp newer than cutoff
            }
        )

        # Step 2: Pull individual expired timestamps from any remaining document
        await database.rate_limits.update_one(
            {"_id": key},
            {"$pull": {"timestamps": {"$lte": cutoff}}}
        )

        # Step 3: READ — count valid timestamps BEFORE writing
        doc = await database.rate_limits.find_one({"_id": key})
        current_timestamps = doc.get("timestamps", []) if doc else []
        current_count = len(current_timestamps)

        logger.info(
            f"[RateLimit/Mongo] DIAGNOSTIC │ "
            f"key={key} │ email={email or '(none)'} │ ip={client_ip or '(none)'} │ "
            f"current_count={current_count} │ max={max_requests} │ window={window_seconds}s │ "
            f"doc_exists={doc is not None} │ "
            f"timestamps_in_window={current_count}"
        )

        # Step 4: Block if at or over the limit — do NOT record this request
        if current_count >= max_requests:
            logger.warning(
                f"[RateLimit/Mongo] BLOCKED │ "
                f"key={key} │ email={email or '(none)'} │ ip={client_ip or '(none)'} │ "
                f"count={current_count} >= max={max_requests} │ DECISION=BLOCK"
            )
            return False

        # Step 5: Allowed — record this request now
        await database.rate_limits.update_one(
            {"_id": key},
            {
                "$push": {"timestamps": now},
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
        logger.info(
            f"[RateLimit/Mongo] ALLOWED │ "
            f"key={key} │ email={email or '(none)'} │ ip={client_ip or '(none)'} │ "
            f"new_count={current_count + 1}/{max_requests} │ DECISION=ALLOW"
        )
        return True

    except Exception as e:
        logger.error(
            f"[RateLimit/Mongo] ERROR │ key={key} email={email or '(none)'} ip={client_ip or '(none)'}: {e}"
        )
        raise  # Let the caller fall through to in-memory


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


def rate_limiter(max_requests: int = 10, window_seconds: int = 60, email_field: str = ""):
    """Returns a dependency that rate-limits per-user (email+IP) or per-IP.

    Args:
        max_requests:  Maximum allowed requests within the window.
        window_seconds: Rolling window size in seconds.
        email_field:   If set, the request body JSON field name that contains
                       the user's email. When provided, the rate-limit key is
                       keyed by  email + IP  (not just IP), so two different
                       users from the same network/NAT are never blocked by
                       each other's requests.

    Fallback chain:  Redis (atomic)  →  MongoDB  →  In-memory
    """
    from redis_client import get_redis

    async def _check_rate_limit(request: Request):
        client_ip = get_client_ip(request)
        endpoint  = request.url.path

        # ------------------------------------------------------------------
        # Extract email from body if requested (for per-user rate limiting)
        # ------------------------------------------------------------------
        email = ""
        if email_field:
            try:
                body_bytes = await request.body()
                if body_bytes:
                    body_json = json.loads(body_bytes)
                    email = str(body_json.get(email_field, "")).strip().lower()
            except Exception:
                pass  # Malformed body — fall back to IP-only key

        # Build key: email+IP when available, otherwise IP-only
        if email:
            key = f"ratelimit:{endpoint}:{email}:{client_ip}"
        else:
            key = f"ratelimit:{endpoint}:{client_ip}"

        # ------------------------------------------------------------------
        # Skip rate limiting for loopback/local IPs
        # ------------------------------------------------------------------
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            logger.debug(f"[RateLimit] Skipped for loopback IP: {client_ip}")
            return

        logger.info(
            f"[RateLimit] INCOMING │ "
            f"IP={client_ip} │ email={email or '(none)'} │ "
            f"endpoint={endpoint} │ key={key} │ "
            f"max={max_requests} │ window={window_seconds}s"
        )

        # ── Layer 1: Redis (atomic INCR) ───────────────────────────────────
        try:
            redis_client = get_redis()
            if redis_client is None:
                raise Exception("Redis connection is unavailable")

            count = await redis_client.incr(key)
            ttl   = await redis_client.ttl(key)

            if ttl == -1:  # Key has no expiry — set it now (first call)
                await redis_client.expire(key, window_seconds)
                ttl = window_seconds

            logger.info(
                f"[RateLimit/Redis] DIAGNOSTIC │ "
                f"key={key} │ count={count} │ ttl={ttl}s │ max={max_requests} │ "
                f"email={email or '(none)'} │ ip={client_ip}"
            )

            if count > max_requests:  # > is correct: INCR already added 1
                logger.warning(
                    f"[RateLimit/Redis] BLOCKED │ "
                    f"key={key} │ count={count} > max={max_requests} │ "
                    f"email={email or '(none)'} │ ip={client_ip} │ DECISION=BLOCK"
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded. Please try again later.",
                )

            request.state.rate_limit_key = key
            request.state.rate_limit_layer = "redis"
            logger.info(
                f"[RateLimit/Redis] ALLOWED │ "
                f"count={count}/{max_requests} │ email={email or '(none)'} │ ip={client_ip} │ DECISION=ALLOW"
            )
            return

        except HTTPException:
            raise
        except Exception as redis_err:
            logger.warning(
                f"[RateLimit/Redis] Unavailable ({redis_err}) → falling back to MongoDB."
            )

        # ── Layer 2: MongoDB ───────────────────────────────────────────────
        try:
            allowed = await check_mongo_rate_limit(
                key, max_requests, window_seconds, email=email, client_ip=client_ip
            )
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded. Please try again later.",
                )
            request.state.rate_limit_key = key
            request.state.rate_limit_layer = "mongo"
            logger.info(
                f"[RateLimit/Mongo] ALLOWED │ "
                f"email={email or '(none)'} │ ip={client_ip} │ DECISION=ALLOW"
            )
            return

        except HTTPException:
            raise
        except Exception as mongo_err:
            logger.error(
                f"[RateLimit/Mongo] Failed ({mongo_err}) → falling back to in-memory."
            )

        # ── Layer 3: In-memory ─────────────────────────────────────────────
        try:
            allowed = check_in_memory_rate_limit(key, max_requests, window_seconds)
            if not allowed:
                logger.warning(
                    f"[RateLimit/Memory] BLOCKED │ "
                    f"key={key} │ email={email or '(none)'} │ ip={client_ip} │ DECISION=BLOCK"
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded. Please try again later.",
                )
            request.state.rate_limit_key = key
            request.state.rate_limit_layer = "memory"
            logger.info(
                f"[RateLimit/Memory] ALLOWED │ "
                f"email={email or '(none)'} │ ip={client_ip} │ DECISION=ALLOW"
            )
        except HTTPException:
            raise
        except Exception as fallback_err:
            logger.error(
                f"[RateLimit/Memory] Failed ({fallback_err}). Bypassing rate limit as last resort."
            )

    return _check_rate_limit


async def rollback_rate_limit(request: Request):
    """Rollback the rate limit increment for the current request (e.g. on downstream SMTP/validation errors)."""
    key = getattr(request.state, "rate_limit_key", None)
    layer = getattr(request.state, "rate_limit_layer", None)

    if not key or not layer:
        return

    logger.info(f"[RateLimit] ROLLING BACK increment for key={key} layer={layer}")

    if layer == "redis":
        try:
            from redis_client import get_redis
            redis_client = get_redis()
            if redis_client:
                count = await redis_client.decr(key)
                if count <= 0:
                    await redis_client.delete(key)
                logger.info(f"[RateLimit/Redis] Rollback successful. New count={count}")
        except Exception as e:
            logger.error(f"[RateLimit/Redis] Rollback failed: {e}")

    elif layer == "mongo":
        try:
            database = get_database()
            if database is not None:
                doc = await database.rate_limits.find_one({"_id": key})
                if doc and doc.get("timestamps"):
                    timestamps = doc["timestamps"]
                    if timestamps:
                        timestamps.pop()  # remove the latest timestamp
                        if timestamps:
                            await database.rate_limits.update_one(
                                {"_id": key},
                                {"$set": {"timestamps": timestamps, "updated_at": datetime.now(timezone.utc)}}
                            )
                        else:
                            await database.rate_limits.delete_one({"_id": key})
                logger.info(f"[RateLimit/Mongo] Rollback successful.")
        except Exception as e:
            logger.error(f"[RateLimit/Mongo] Rollback failed: {e}")

    elif layer == "memory":
        try:
            if key in FALLBACK_STORE and FALLBACK_STORE[key]:
                FALLBACK_STORE[key].pop()
                if not FALLBACK_STORE[key]:
                    del FALLBACK_STORE[key]
            logger.info(f"[RateLimit/Memory] Rollback successful.")
        except Exception as e:
            logger.error(f"[RateLimit/Memory] Rollback failed: {e}")


async def reset_rate_limit_for_email(email: str):
    """Reset all endpoint rate limits for a specific email in both Redis and MongoDB."""
    email_clean = email.strip().lower()
    
    # 1. Clear MongoDB rate_limits
    try:
        database = get_database()
        if database is not None:
            # Matches any key containing the email formatted in dependencies
            result = await database.rate_limits.delete_many(
                {"_id": {"$regex": f".*:{email_clean}:.*"}}
            )
            # Support any legacy formats ending with the email
            legacy_result = await database.rate_limits.delete_many(
                {"_id": {"$regex": f".*:{email_clean}$"}}
            )
            total_deleted = result.deleted_count + legacy_result.deleted_count
            if total_deleted:
                logger.info(f"[RateLimit] Cleared {total_deleted} rate limit documents from MongoDB for email: {email_clean}")
    except Exception as e:
        logger.error(f"[RateLimit] Failed to clear MongoDB rate limits for {email_clean}: {e}")
        
    # 2. Clear Redis keys
    try:
        from redis_client import get_redis
        redis_client = get_redis()
        if redis_client:
            pattern = f"*:{email_clean}:*"
            keys = await redis_client.keys(pattern)
            # Legacy pattern
            legacy_pattern = f"*:{email_clean}"
            legacy_keys = await redis_client.keys(legacy_pattern)
            
            all_keys = list(set(keys + legacy_keys))
            if all_keys:
                await redis_client.delete(*all_keys)
                logger.info(f"[RateLimit] Cleared {len(all_keys)} keys from Redis matching email: {email_clean}")
    except Exception as e:
        logger.error(f"[RateLimit] Failed to clear Redis rate limits for {email_clean}: {e}")


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

