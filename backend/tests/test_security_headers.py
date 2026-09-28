"""Browser security headers (nginx) and the CSP report collector."""
import logging
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.csp_report as csp

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ["https.conf", "http-only.conf"]


def _conf(name: str) -> str:
    return (ROOT / "deploy" / "nginx" / "templates" / name).read_text()


@pytest.mark.parametrize("name", TEMPLATES)
def test_templates_send_security_headers(name):
    conf = _conf(name)
    assert 'add_header X-Content-Type-Options "nosniff" always;' in conf
    assert 'add_header Referrer-Policy "strict-origin-when-cross-origin" always;' in conf
    assert "add_header Content-Security-Policy-Report-Only" in conf
    assert "report-uri /api/csp-report" in conf


@pytest.mark.parametrize("name", TEMPLATES)
def test_csp_is_warn_only(name):
    # Enforcing header must not appear until the report logs are clean.
    assert not re.search(r"add_header\s+Content-Security-Policy\s", _conf(name))


@pytest.mark.parametrize("name", TEMPLATES)
def test_no_location_level_add_header(name):
    """An add_header inside a location silently drops every server-level
    header for that location, which would strip the headers above."""
    in_location = False
    for line in _conf(name).splitlines():
        s = line.strip()
        if s.startswith("location "):
            in_location = True
        elif s == "}" and in_location:
            in_location = False
        elif in_location:
            assert not s.startswith("add_header"), line


@pytest.fixture
def client():
    csp._last_logged.clear()
    app = FastAPI()
    app.include_router(csp.router)
    return TestClient(app)


LEGACY = {"csp-report": {"document-uri": "https://vivoops.example/alerts",
                         "effective-directive": "img-src",
                         "blocked-uri": "https://tracker.example/pixel"}}


def test_report_is_logged_once_per_violation(client, caplog):
    with caplog.at_level(logging.INFO, logger=csp.log.name):
        for _ in range(3):
            r = client.post("/csp-report", json=LEGACY,
                            headers={"content-type": "application/csp-report"})
            assert r.status_code == 204
    assert caplog.text.count("CSP report-only violation") == 1
    assert "directive=img-src" in caplog.text
    assert "blocked=https://tracker.example/pixel" in caplog.text


def test_reporting_api_format(client, caplog):
    body = [{"type": "csp-violation",
             "body": {"effectiveDirective": "script-src-elem",
                      "blockedURL": "inline", "documentURL": "https://x/"}}]
    with caplog.at_level(logging.INFO, logger=csp.log.name):
        assert client.post("/csp-report", json=body).status_code == 204
    assert "directive=script-src-elem" in caplog.text


def test_oversized_body_refused(client, caplog):
    big = b"x" * (csp.MAX_BODY_BYTES + 1)
    with caplog.at_level(logging.INFO, logger=csp.log.name):
        r = client.post("/csp-report", content=big)
    assert r.status_code == 413
    assert "CSP report-only violation" not in caplog.text


def test_garbage_is_ignored(client):
    assert client.post("/csp-report", content=b"not json").status_code == 204


def test_logged_values_are_single_line_and_truncated(client, caplog):
    evil = {"csp-report": {"effective-directive": "img-src",
                           "blocked-uri": "https://a/\nFAKE LOG LINE " + "y" * 500}}
    with caplog.at_level(logging.INFO, logger=csp.log.name):
        client.post("/csp-report", json=evil)
    line = [r.getMessage() for r in caplog.records][0]
    assert "\n" not in line
    assert len(line) < 400
