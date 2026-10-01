"""Shared Redis connection, used for caching, rate limiting, and chat pub/sub."""

import json
import logging
import os

import redis
import redis.asyncio as redis_asyncio
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Chat's websocket handler is async and needs a non-blocking client. Every
# other caller here is a normal sync endpoint and uses the client above.
async_redis_client = redis_asyncio.from_url(REDIS_URL, decode_responses=True)

ROOMS_KEY = "cache:rooms"
ROOMS_TTL = 300           # rooms rarely change
AVAILABILITY_TTL = 30     # short, since this feeds a booking decision


# Caching is an optimization, not the source of truth -- Postgres is. If
# Redis is down, every function here just falls back to "nothing cached"
# instead of raising, so a Redis outage doesn't take the API down with it.
def _redis_down(action, exc):
    logger.warning("redis unavailable, %s", action, exc_info=exc)


def cache_get(key):
    try:
        value = redis_client.get(key)
    except redis.exceptions.RedisError as exc:
        _redis_down("treating as a cache miss", exc)
        return None
    return json.loads(value) if value is not None else None


def cache_set(key, value, ttl):
    try:
        redis_client.setex(key, ttl, json.dumps(value))
    except redis.exceptions.RedisError as exc:
        _redis_down("skipping cache write", exc)


def invalidate_rooms():
    try:
        redis_client.delete(ROOMS_KEY)
    except redis.exceptions.RedisError as exc:
        _redis_down("skipping cache invalidation", exc)


def availability_key(starts_at, ends_at, min_capacity):
    """Includes a generation number so a booking/cancellation can invalidate
    every cached availability result at once, just by bumping the counter --
    no need to know or scan for every possible key."""
    try:
        gen = redis_client.get("cache:avail:gen") or "0"
    except redis.exceptions.RedisError as exc:
        _redis_down("skipping availability cache", exc)
        gen = "down"  # no real generation matches this, so it just always misses
    return f"cache:avail:{gen}:{starts_at.isoformat()}:{ends_at.isoformat()}:{min_capacity}"


def invalidate_availability():
    """Call this after any booking or cancellation. Old cached results become
    unreachable and just expire on their own TTL."""
    try:
        redis_client.incr("cache:avail:gen")
    except redis.exceptions.RedisError as exc:
        _redis_down("skipping cache invalidation", exc)


if __name__ == "__main__":
    print(redis_client.ping())
