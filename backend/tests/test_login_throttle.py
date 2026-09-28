"""Failed-login throttling: per person, per account, per connection."""
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.auth.login_throttle as lt
from app.auth import routes
from app.auth.security import hash_password


class _FakeRedis:
    """INCR/EXPIRE/TTL/GET/DELETE with a manual clock for expiry."""

    def __init__(self):
        self.now = 0.0
        self.vals: dict[str, int] = {}
        self.exp: dict[str, float] = {}
        self.down = False

    def _check(self):
        if self.down:
            raise ConnectionError("redis down")

    def _live(self, key):
        if key in self.exp and self.exp[key] <= self.now:
            self.vals.pop(key, None)
            self.exp.pop(key, None)
        return key in self.vals

    def get(self, key):
        self._check()
        return str(self.vals[key]) if self._live(key) else None

    def incr(self, key):
        self._check()
        self._live(key)
        self.vals[key] = self.vals.get(key, 0) + 1
        return self.vals[key]

    def expire(self, key, seconds):
        self._check()
        self.exp[key] = self.now + seconds

    def ttl(self, key):
        self._check()
        if not self._live(key):
            return -2
        return int(self.exp[key] - self.now) if key in self.exp else -1

    def delete(self, key):
        self._check()
        self.vals.pop(key, None)
        self.exp.pop(key, None)


@pytest.fixture
def r(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(lt, "_redis", lambda: fake)
    monkeypatch.setattr(lt.settings, "login_throttle_enabled", True)
    monkeypatch.setattr(lt.settings, "login_fail_person_limit", 5)
    monkeypatch.setattr(lt.settings, "login_fail_person_window_s", 900)
    monkeypatch.setattr(lt.settings, "login_fail_account_limit", 20)
    monkeypatch.setattr(lt.settings, "login_fail_account_window_s", 3600)
    monkeypatch.setattr(lt.settings, "login_fail_ip_limit", 100)
    monkeypatch.setattr(lt.settings, "login_fail_ip_window_s", 900)
    return fake


STORE_IP = "198.51.100.7"


def _fail(email, ip, n):
    for _ in range(n):
        lt.record_failure(email, ip)


# ── the throttle rules ────────────────────────────────────────────────

def test_person_blocked_after_five_failures(r):
    _fail("a@x.com", STORE_IP, 4)
    assert lt.seconds_blocked("a@x.com", STORE_IP) == 0
    lt.record_failure("a@x.com", STORE_IP)
    assert lt.seconds_blocked("a@x.com", STORE_IP) == 900


def test_colleague_on_same_connection_not_blocked(r):
    _fail("typo@x.com", STORE_IP, 5)
    assert lt.seconds_blocked("typo@x.com", STORE_IP) > 0
    assert lt.seconds_blocked("colleague@x.com", STORE_IP) == 0


def test_block_expires_after_window(r):
    _fail("a@x.com", STORE_IP, 5)
    r.now += 901
    assert lt.seconds_blocked("a@x.com", STORE_IP) == 0


def test_email_is_case_insensitive(r):
    _fail("A@X.com ", STORE_IP, 5)
    assert lt.seconds_blocked("a@x.com", STORE_IP) > 0


def test_success_clears_person_counter(r):
    _fail("a@x.com", STORE_IP, 4)
    lt.record_success("a@x.com", STORE_IP)
    lt.record_failure("a@x.com", STORE_IP)
    assert lt.seconds_blocked("a@x.com", STORE_IP) == 0


def test_account_blocked_when_guessed_from_many_connections(r):
    for i in range(20):
        lt.record_failure("boss@x.com", f"10.0.0.{i}")
    assert lt.seconds_blocked("boss@x.com", "10.9.9.9") == 3600


def test_connection_blocked_when_trying_many_accounts(r):
    for i in range(100):
        lt.record_failure(f"user{i}@x.com", "203.0.113.5")
    assert lt.seconds_blocked("new@x.com", "203.0.113.5") > 0
    assert lt.seconds_blocked("new@x.com", STORE_IP) == 0


def test_redis_down_fails_open_and_logs(r, caplog):
    r.down = True
    with caplog.at_level(logging.WARNING, logger=lt.log.name):
        lt.record_failure("a@x.com", STORE_IP)
        assert lt.seconds_blocked("a@x.com", STORE_IP) == 0
    assert "login throttle" in caplog.text


def test_disabled_never_blocks(r, monkeypatch):
    monkeypatch.setattr(lt.settings, "login_throttle_enabled", False)
    _fail("a@x.com", STORE_IP, 50)
    assert lt.seconds_blocked("a@x.com", STORE_IP) == 0


def test_client_ip_prefers_nginx_real_ip():
    req = Request({"type": "http", "headers": [(b"x-real-ip", b"198.51.100.7")],
                   "client": ("172.18.0.5", 5000)})
    assert lt.client_ip(req) == "198.51.100.7"
    req = Request({"type": "http", "headers": [], "client": ("172.18.0.5", 5000)})
    assert lt.client_ip(req) == "172.18.0.5"


# ── the /auth/login route ─────────────────────────────────────────────

class _Query:
    def __init__(self, user):
        self.user = user

    def filter(self, *_a):
        return self

    def first(self):
        return self.user


class _DB:
    def __init__(self, user):
        self.user = user

    def query(self, _model):
        return _Query(self.user)

    def commit(self):
        pass


@pytest.fixture
def user():
    return SimpleNamespace(id=1, email="op@x.com", role="operator",
                           is_active=True, last_login_at=None,
                           password_hash=hash_password("right-password"))


def _req(ip=STORE_IP):
    return Request({"type": "http", "headers": [(b"x-real-ip", ip.encode())],
                    "client": ("172.18.0.5", 5000)})


def _login(db, password, ip=STORE_IP):
    return routes.login(routes.LoginIn(email="op@x.com", password=password),
                        _req(ip), db)


def test_login_blocks_with_429_after_repeated_failures(r, user):
    db = _DB(user)
    for _ in range(5):
        with pytest.raises(HTTPException) as e:
            _login(db, "wrong")
        assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        _login(db, "right-password")      # blocked even with the right password
    assert e.value.status_code == 429
    assert e.value.headers["Retry-After"] == "900"
    assert e.value.detail == "Too many failed attempts. Try again in 15 minutes."


def test_unknown_account_failures_are_counted(r):
    db = _DB(None)
    for _ in range(5):
        with pytest.raises(HTTPException):
            _login(db, "guess")
    assert lt.seconds_blocked("op@x.com", STORE_IP) > 0


def test_successful_login_resets_counter(r, user):
    db = _DB(user)
    for _ in range(4):
        with pytest.raises(HTTPException):
            _login(db, "wrong")
    assert _login(db, "right-password").role == "operator"
    with pytest.raises(HTTPException) as e:
        _login(db, "wrong")
    assert e.value.status_code == 401    # counter restarted, not blocked
