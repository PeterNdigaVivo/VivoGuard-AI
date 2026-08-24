from app.stream import auto_transport


def _camera(*, transport: str = "auto") -> dict:
    return {
        "host": "camera.example.test",
        "rtsp_port": 554,
        "http_port": 80,
        "transport": transport,
        "username": "operator",
        "channel_number": 2,
        "brand": "dahua",
    }


def test_legacy_auto_transport_negotiates_working_snapshot(monkeypatch) -> None:
    auto_transport._PROBED.clear()
    monkeypatch.setattr(
        auto_transport,
        "_tcp_reachable",
        lambda _host, port, timeout=2.0: port == 80,
    )
    monkeypatch.setattr(
        auto_transport,
        "probe_rtsp_tunnel",
        lambda **_kwargs: {"working_port": None, "attempts": []},
    )
    monkeypatch.setattr(
        auto_transport,
        "_try_snapshot",
        lambda url, _username, _password: (
            "cgi-bin/snapshot.cgi" in url and "channel=2" in url,
            "JPEG ok",
        ),
    )

    assert auto_transport.negotiate(_camera(), "secret") == {
        "transport": "http_snapshot",
    }


def test_explicit_snapshot_transport_is_not_renegotiated(monkeypatch) -> None:
    auto_transport._PROBED.clear()
    called = False

    def tcp_probe(*_args, **_kwargs):
        nonlocal called
        called = True
        return False

    monkeypatch.setattr(auto_transport, "_tcp_reachable", tcp_probe)

    assert auto_transport.negotiate(
        _camera(transport="http_snapshot"), "secret",
    ) is None
    assert not called
