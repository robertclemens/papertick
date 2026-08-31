"""Redis-backed fixed-window rate limiting and login lockout counters."""

import redis
from fastapi import HTTPException, Request

from app.config import get_settings

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(key: str, limit: int, window_seconds: int) -> None:
    """Raise 429 when `key` exceeds `limit` hits per window. Fails open if Redis is down."""
    try:
        r = get_redis()
        bucket = f"rl:{key}:{window_seconds}"
        n = r.incr(bucket)
        if n == 1:
            r.expire(bucket, window_seconds)
        if n > limit:
            raise HTTPException(status_code=429, detail="Too many requests, slow down")
    except redis.RedisError:
        return


def rate_limiter(name: str, limit: int, window_seconds: int):
    def dependency(request: Request) -> None:
        enforce_rate_limit(f"{name}:{client_ip(request)}", limit, window_seconds)

    return dependency


# ------------------------------------------------------------- login lockout

def _lock_key(email: str) -> str:
    return f"lockout:{email.lower()}"


def is_locked_out(email: str) -> bool:
    try:
        r = get_redis()
        v = r.get(_lock_key(email))
        return v is not None and int(v) >= get_settings().login_max_failures
    except redis.RedisError:
        return False


def record_login_failure(email: str) -> None:
    try:
        r = get_redis()
        k = _lock_key(email)
        n = r.incr(k)
        if n == 1:
            r.expire(k, get_settings().login_lockout_seconds)
    except redis.RedisError:
        pass


def clear_login_failures(email: str) -> None:
    try:
        get_redis().delete(_lock_key(email))
    except redis.RedisError:
        pass
