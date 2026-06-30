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


def _json_default(obj: Any):
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class RedisClient:
    client: Optional[Redis] = None

redis_client = RedisClient()

async def connect_redis() -> None:
    """Initialize Redis connection (called at app lifespan startup)."""
    settings = get_settings()
    redis_client.client = redis.from_url(
        f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}",
        username=settings.redis_username,
        password=settings.redis_password or None,
        decode_responses=True,
    )

async def close_redis() -> None:
    """Close Redis connection (called at app lifespan shutdown)."""
    if redis_client.client:
        await redis_client.client.close()
        redis_client.client = None

def get_redis() -> Redis:
    """Get the Redis client instance. Call only after connect_redis()."""
    if redis_client.client is None:
        raise RuntimeError("Redis client not initialized. Call connect_redis() first.")
    return redis_client.client


# ─── High-level cache helpers ────────────────────────────────────────────────

_CACHE_PREFIX = "inbox"

def _key(*parts: str) -> str:
    """Build a namespaced Redis key."""
    return ":".join([_CACHE_PREFIX, *parts])


async def cache_get(key: str) -> Optional[dict]:
    """Get a cached JSON value. Returns None if missing or decode fails."""
    r = get_redis()
    raw = await r.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    """Set a cached JSON value with TTL."""
    r = get_redis()
    await r.set(key, json.dumps(value, default=_json_default), ex=ttl_seconds)


async def cache_delete(key: str) -> None:
    """Delete a specific cache key."""
    r = get_redis()
    await r.delete(key)


async def cache_delete_pattern(pattern: str) -> None:
    """Delete all keys matching a pattern (e.g. 'inbox:user:123:*')."""
    r = get_redis()
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor, match=pattern, count=100)
        if keys:
            await r.delete(*keys)
        if cursor == 0:
            break
