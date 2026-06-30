"""
Redis client singleton for caching.
Follows the same pattern as database.py — lifespan-managed singleton.
"""
import json
from datetime import datetime
from typing import Optional, Any
import redis.asyncio as redis
from redis.asyncio import Redis
from config import get_settings

_settings = get_settings()


from enum import Enum
from bson import ObjectId
from pydantic import BaseModel
from datetime import date

def _json_default(obj: Any):
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class RedisClient:
    client: Optional[Redis] = None

redis_client = RedisClient()

async def connect_redis() -> None:
    """Initialize Redis connection (called at app lifespan startup).

    Connection strategy (priority order):
      1. REDIS_URL env var  — full URL (e.g. rediss://... from Render managed Redis with TLS)
      2. REDIS_HOST + REDIS_PORT + REDIS_PASSWORD — for local dev / custom Redis
    """
    settings = get_settings()

    if settings.redis_url:
        # Prefer the full URL — Render provides this as rediss:// with TLS embedded
        connection_url = settings.redis_url
        logger.info(f"[Redis] Connecting via REDIS_URL (TLS): {connection_url[:40]}...")
        redis_client.client = redis.from_url(
            connection_url,
            decode_responses=True,
        )
    else:
        # Build URL from individual parts (local dev / custom Redis)
        connection_url = (
            f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"
        )
        logger.info(
            f"[Redis] Connecting via host/port: {settings.redis_host}:{settings.redis_port} "
            f"db={settings.redis_db}"
        )
        redis_client.client = redis.from_url(
            connection_url,
            username=settings.redis_username,
            password=settings.redis_password or None,
            decode_responses=True,
        )

    try:
        await redis_client.client.ping()
        logger.info("[Redis] Successfully connected (ping OK).")
    except Exception as e:
        logger.warning(
            f"[Redis] Connection failed ({e}). "
            "Rate limiting will fall back to MongoDB / in-memory."
        )
        redis_client.client = None

async def close_redis() -> None:
    """Close Redis connection (called at app lifespan shutdown)."""
    if redis_client.client:
        await redis_client.client.close()
        redis_client.client = None

def get_redis() -> Optional[Redis]:
    """Get the Redis client instance. Returns None if Redis is unavailable."""
    return redis_client.client


# ─── High-level cache helpers ────────────────────────────────────────────────

_CACHE_PREFIX = "inbox"
import logging
logger = logging.getLogger(__name__)

def _key(*parts: str) -> str:
    """Build a namespaced Redis key."""
    return ":".join([_CACHE_PREFIX, *parts])


async def cache_get(key: str) -> Optional[dict]:
    """Get a cached JSON value. Returns None if missing, decode fails, or Redis fails."""
    try:
        r = get_redis()
        if r is None:
            return None
        raw = await r.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Redis cache_get error for key '{key}': {e}")
        return None


async def cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    """Set a cached JSON value with TTL. Fails silently if Redis is down."""
    try:
        r = get_redis()
        if r is None:
            return
        await r.set(key, json.dumps(value, default=_json_default), ex=ttl_seconds)
    except Exception as e:
        logger.warning(f"Redis cache_set error for key '{key}': {e}")


async def cache_delete(key: str) -> None:
    """Delete a specific cache key. Fails silently if Redis is down."""
    try:
        r = get_redis()
        if r is None:
            return
        await r.delete(key)
    except Exception as e:
        logger.warning(f"Redis cache_delete error for key '{key}': {e}")


async def cache_delete_pattern(pattern: str) -> None:
    """Delete all keys matching a pattern. Fails silently if Redis is down."""
    try:
        r = get_redis()
        if r is None:
            return
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor, match=pattern, count=100)
            if keys:
                await r.delete(*keys)
            if cursor == 0:
                break
    except Exception as e:
        logger.warning(f"Redis cache_delete_pattern error for pattern '{pattern}': {e}")
