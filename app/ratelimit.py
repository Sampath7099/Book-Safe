"""Simple rate limiting: how many times has this key acted recently? Too
many -> 429. Fixed window counter in Redis (INCR + EXPIRE), not a sliding
window -- good enough to stop brute-forcing /login or hammering /bookings.

    /auth/login  -- by IP, before we know who's logging in
    /bookings    -- by user, after auth
    chat send    -- by user, called directly from the websocket handler
"""

import logging
import time

import redis
from fastapi import Depends, HTTPException, Request

from app.auth import current_user
from app.cache import redis_client

logger = logging.getLogger(__name__)


def allow(scope: str, identity: str, limit: int, window_seconds: int) -> int | None:
    """Records one action. Returns None if it's allowed, or seconds until
    the caller can retry if not."""
    window = int(time.time()) // window_seconds
    key = f"rl:{scope}:{identity}:{window}"

    try:
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, window_seconds)
    except redis.exceptions.RedisError:
        # If Redis is down, let the request through rather than failing
        # login/booking because a secondary system is unavailable.
        logger.warning("redis unavailable, allowing request through unlimited",
                       exc_info=True)
        return None

    if count > limit:
        return window_seconds - (int(time.time()) % window_seconds)
    return None


def _enforce(scope: str, identity: str, limit: int, window_seconds: int):
    retry_after = allow(scope, identity, limit, window_seconds)
    if retry_after is not None:
        raise HTTPException(
            429, "too many requests, slow down",
            headers={"Retry-After": str(retry_after)},
        )


def by_ip(scope: str, limit: int, window_seconds: int):
    """For routes with no logged-in user yet, e.g. login."""
    def dependency(request: Request):
        ip = request.client.host if request.client else "unknown"
        _enforce(scope, ip, limit, window_seconds)
    return dependency


def by_user(scope: str, limit: int, window_seconds: int):
    """For routes behind auth."""
    def dependency(user: dict = Depends(current_user)):
        _enforce(scope, str(user["id"]), limit, window_seconds)
    return dependency
