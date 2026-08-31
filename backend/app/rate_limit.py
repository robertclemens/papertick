"""Redis-backed fixed-window rate limiting and login lockout counters."""

import logging
from functools import lru_cache
from ipaddress import ip_address, ip_network

import redis
from fastapi import HTTPException, Request

from app.config import get_settings

log = logging.getLogger("papertick.ratelimit")

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


@lru_cache
def _trusted_proxies() -> tuple:
    nets = []
    for raw in get_settings().trusted_proxy_cidrs.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            nets.append(ip_network(raw, strict=False))
        except ValueError:
            log.error("ignoring malformed TRUSTED_PROXY_CIDRS entry %r", raw)
    return tuple(nets)


def _trusted(addr: str) -> bool:
    nets = _trusted_proxies()
    if not nets:
        return False
    try:
        ip = ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in nets)


def client_ip(request: Request) -> str:
    """The caller's address, as far as it can be trusted.

    `X-Forwarded-For` is a chain the client starts: the left-most entry is
    whatever the *client* sent, and only the right-most hops were appended by
    infrastructure. Taking the left-most entry hands the rate-limit bucket key
    to the attacker — rotating the header defeats every limiter in the app.

    So: a direct connection's header is ignored outright, and behind a proxy we
    walk the chain from the right, skipping hops we ourselves trust, and stop at
    the first address the client could have forged. With no TRUSTED_PROXY_CIDRS
    configured, nothing is trusted and only the peer address is ever used.
    """
    peer = request.client.host if request.client else "unknown"
    if not _trusted(peer):
        return peer
    chain = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(chain):
        if not _trusted(hop):
            try:
                ip_address(hop)
            except ValueError:
                break          # malformed entry: trust nothing further left
            return hop
    return peer


def enforce_rate_limit(key: str, limit: int, window_seconds: int,
                       fail_open: bool = True) -> None:
    """Raise 429 when `key` exceeds `limit` hits per window.

    `fail_open=False` (authentication paths) turns a Redis outage into a 503
    rather than an open door — an attacker who can knock Redis over must not
    thereby switch off brute-force protection.
    """
    try:
        r = get_redis()
        bucket = f"rl:{key}:{window_seconds}"
        n = r.incr(bucket)
        if n == 1:
            r.expire(bucket, window_seconds)
        if n > limit:
            raise HTTPException(status_code=429, detail="Too many requests, slow down")
    except redis.RedisError as exc:
        log.error("rate-limit backend unavailable for %s: %s", key, exc)
        if not fail_open:
            raise HTTPException(
                status_code=503,
                detail="Authentication is temporarily unavailable, try again shortly",
            )


def rate_limiter(name: str, limit: int, window_seconds: int, fail_open: bool = True):
    def dependency(request: Request) -> None:
        enforce_rate_limit(f"{name}:{client_ip(request)}", limit, window_seconds, fail_open)

    return dependency


def enforce_account_limit(name: str, identity: str, limit: int,
                          window_seconds: int) -> None:
    """Rate limit keyed on an account identity rather than a source address.

    Behind a proxy that cannot be trusted to report the caller (see
    `client_ip`), every request shares one per-IP bucket, so a per-IP limit
    alone lets one abusive caller exhaust the allowance for everyone. Keying on
    the address being acted upon restores per-target fairness, and an identity
    cannot be rotated as freely as a header.
    """
    enforce_rate_limit(f"{name}:acct:{identity.lower()}", limit, window_seconds)


# ------------------------------------------------------------- login lockout

def _lock_key(email: str, ip: str) -> str:
    """Lockout is scoped to (account, source).

    Keying on the email alone lets anyone who knows an address lock its owner
    out indefinitely — ten failed logins every fifteen minutes is a permanent
    denial of service against a named victim. Scoping to the source address
    keeps the brute-force brake (an attacker slows their own origin down) while
    removing the ability to lock out someone else.
    """
    return f"lockout:{email.lower()}:{ip}"


def is_locked_out(email: str, ip: str) -> bool:
    try:
        v = get_redis().get(_lock_key(email, ip))
        return v is not None and int(v) >= get_settings().login_max_failures
    except redis.RedisError as exc:
        log.error("lockout backend unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Authentication is temporarily unavailable, try again shortly",
        )


def record_login_failure(email: str, ip: str) -> None:
    try:
        r = get_redis()
        k = _lock_key(email, ip)
        n = r.incr(k)
        # Re-arm the window on every failure so a sustained attack from one
        # source stays locked instead of lapsing on the first failure's clock.
        r.expire(k, get_settings().login_lockout_seconds)
        if n == get_settings().login_max_failures:
            log.warning("login lockout engaged for %s from %s", email, ip)
    except redis.RedisError:
        pass


def clear_login_failures(email: str, ip: str) -> None:
    try:
        get_redis().delete(_lock_key(email, ip))
    except redis.RedisError:
        pass
