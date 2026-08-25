import asyncio
from types import SimpleNamespace

from app.api import websockets


class _Socket:
    def __init__(self, protocols: str = ""):
        self.headers = {"sec-websocket-protocol": protocols}
        self.accepted = None
        self.closed = None

    async def accept(self, *, subprotocol=None):
        self.accepted = subprotocol

    async def close(self, *, code=1000, reason=None):
        self.closed = (code, reason)


class _Session:
    def __init__(self, user):
        self.user = user
        self.expunge_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, _model, _user_id):
        return self.user

    def expunge(self, user):
        self.expunge_calls.append(user)


def _run(socket, *, roles=None):
    return asyncio.run(websockets._authenticate(socket, roles=roles))


def test_websocket_without_bearer_protocol_is_rejected(monkeypatch):
    monkeypatch.setattr(websockets, "SessionLocal", lambda: (_ for _ in ()).throw(
        AssertionError("database must not be queried without credentials")))
    socket = _Socket()

    assert _run(socket) is None
    assert socket.accepted is None
    assert socket.closed == (4401, "authentication required")


def test_websocket_rejects_refresh_token(monkeypatch):
    monkeypatch.setattr(websockets, "decode_token", lambda _token: {
        "sub": "7", "kind": "refresh",
    })
    monkeypatch.setattr(websockets, "SessionLocal", lambda: (_ for _ in ()).throw(
        AssertionError("database must not be queried for an invalid token")))
    socket = _Socket("vg-token, refresh.jwt")

    assert _run(socket) is None
    assert socket.closed == (4401, "invalid token")


def test_websocket_rejects_disabled_user(monkeypatch):
    user = SimpleNamespace(id=7, role="operator", is_active=False)
    monkeypatch.setattr(websockets, "decode_token", lambda _token: {
        "sub": "7", "kind": "access",
    })
    monkeypatch.setattr(websockets, "SessionLocal", lambda: _Session(user))
    socket = _Socket("vg-token, access.jwt")

    assert _run(socket) is None
    assert socket.accepted is None
    assert socket.closed == (4401, "user disabled")


def test_training_websocket_rejects_viewer_role(monkeypatch):
    user = SimpleNamespace(id=7, role="viewer", is_active=True)
    monkeypatch.setattr(websockets, "decode_token", lambda _token: {
        "sub": "7", "kind": "access",
    })
    monkeypatch.setattr(websockets, "SessionLocal", lambda: _Session(user))
    socket = _Socket("vg-token, access.jwt")

    assert _run(socket, roles={"admin", "operator"}) is None
    assert socket.accepted is None
    assert socket.closed == (4403, "insufficient role")


def test_active_user_websocket_accepts_only_fixed_protocol(monkeypatch):
    user = SimpleNamespace(id=7, role="viewer", is_active=True)
    session = _Session(user)
    monkeypatch.setattr(websockets, "decode_token", lambda token: {
        "sub": "7", "kind": "access", "token_seen": token,
    })
    monkeypatch.setattr(websockets, "SessionLocal", lambda: session)
    socket = _Socket("vg-token, access.jwt")

    assert _run(socket) is user
    assert socket.accepted == "vg-token"
    assert socket.closed is None
    assert session.expunge_calls == [user]
