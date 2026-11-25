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

    assert "http://127.0.0.1/api/healthz" in deploy
    assert "wget --no-check-certificate" in deploy
    assert "nginx cannot reach the healthy API" in deploy


def test_deploy_gate_waits_for_inference_coverage_recovery():
    root = Path(__file__).resolve().parents[2]
    deploy = (root / "scripts" / "deploy.sh").read_text()

    assert "vg:inference:health" in deploy
    assert "docker compose exec -T api python -m app.utils.inference_recovery" in deploy
    assert "inference coverage failed to recover after deploy" in deploy


def test_deploy_inference_timeout_uses_aggregate_diagnostics():
    root = Path(__file__).resolve().parents[2]
    deploy = (root / "scripts" / "deploy.sh").read_text()
    timeout = deploy.split('echo "inference coverage failed to recover after deploy"', 1)[1]
    timeout = timeout.split("exit 1", 1)[0]

    assert "python -m app.utils.inference_recovery --summary" in timeout
    assert "docker compose logs" not in timeout
    assert "latest inference health: %s" not in timeout
