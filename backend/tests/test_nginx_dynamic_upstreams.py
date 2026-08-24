from pathlib import Path


def test_nginx_resolves_compose_upstreams_dynamically():
    root = Path(__file__).resolve().parents[2]
    config = (root / "deploy" / "nginx" / "nginx.conf").read_text()

    assert "resolver 127.0.0.11" in config
    assert "proxy_pass $api_upstream;" in config
    assert "proxy_pass $frontend_upstream;" in config
    assert "proxy_pass http://api:8000/;" not in config
    assert "proxy_pass http://frontend:80/;" not in config


def test_deploy_gate_checks_public_proxy_path():
    root = Path(__file__).resolve().parents[2]
    deploy = (root / "scripts" / "deploy.sh").read_text()

    assert "http://localhost/api/healthz" in deploy
    assert "nginx cannot reach the healthy API" in deploy
