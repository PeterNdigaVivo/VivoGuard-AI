"""Failed-login throttling — slows password guessing on /auth/login.

Three independent counters in Redis, each a fixed window that starts at
the first failure and blocks for the rest of the window once its limit
is reached:

  person   one account from one connection   (default 5 / 15 min)
  account  one account from anywhere         (default 20 / 1 h)
  ip       any account from one connection   (default 100 / 15 min)

Why three: staff at one store share an internet connection, so keying
only on the IP would let one person's typos lock out the whole shop.
The tight limit is therefore per account *and* connection; the account
rule catches guessing spread over many connections, and the high IP rule
catches one connection trying many accounts — far above what a store's
normal logins ever reach.

A successful login clears that person's counter. If Redis is down the
throttle fails OPEN (logins proceed, a warning is logged): a cache outage
must not lock every operator out of a security system.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Request

from app.config import settings

log = logging.getLogger(__name__)

_PREFIX = "vg:login:fail"


@dataclass(frozen=True)
class _Rule:
    name: str
    limit: int
    window_s: int

    def key(self, email: str, ip: str) -> str:
        if self.name == "person":
            return f"{_PREFIX}:person:{email}|{ip}"
        if self.name == "account":
            return f"{_PREFIX}:account:{email}"
        return f"{_PREFIX}:ip:{ip}"


def _rules() -> list[_Rule]:
    return [
        _Rule("person", settings.login_fail_person_limit,
              settings.login_fail_person_window_s),
        _Rule("account", settings.login_fail_account_limit,
              settings.login_fail_account_window_s),
        _Rule("ip", settings.login_fail_ip_limit,
              settings.login_fail_ip_window_s),
    ]


def _redis():
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True,
                          socket_timeout=2)


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def client_ip(request: Request) -> str:
    """The caller's address. nginx sets X-Real-IP to $remote_addr and
    overwrites anything the client sent, so it cannot be spoofed —
    unlike the left-most X-Forwarded-For entry uvicorn would pick."""
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    return request.client.host if request.client else "unknown"


def seconds_blocked(email: str, ip: str) -> int:
    """0 when this login may proceed, else seconds until it may retry."""
    if not settings.login_throttle_enabled:
        return 0
    email = normalise_email(email)
    try:
        r = _redis()
        wait = 0
        for rule in _rules():
            key = rule.key(email, ip)
            count = int(r.get(key) or 0)
            if count >= rule.limit:
                ttl = int(r.ttl(key) or 0)
                wait = max(wait, ttl if ttl > 0 else rule.window_s)
        return wait
    except Exception as exc:
        log.warning("login throttle unavailable (%s) — allowing login", exc)
        return 0


def record_failure(email: str, ip: str) -> None:
    if not settings.login_throttle_enabled:
        return
    email = normalise_email(email)
    try:
        r = _redis()
        for rule in _rules():
            key = rule.key(email, ip)
            if int(r.incr(key)) == 1:
                r.expire(key, rule.window_s)
    except Exception as exc:
        log.warning("login throttle: could not record failure (%s)", exc)


def record_success(email: str, ip: str) -> None:
    """Clear the person counter. The account and IP counters are left to
    expire so a successful login cannot be used to reset a wider attack."""
    if not settings.login_throttle_enabled:
        return
    email = normalise_email(email)
    try:
        _redis().delete(_rules()[0].key(email, ip))
    except Exception as exc:
        log.warning("login throttle: could not clear counter (%s)", exc)
