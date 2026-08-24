from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_gpu_override_is_isolated_to_inference():
    profile = (ROOT / "docker-compose.gpu.yml").read_text()

    assert "vivoguard/worker:cuda" in profile
    assert "GPU_BACKEND: cuda" in profile
    assert 'USE_GPU: "true"' in profile
    assert "driver: nvidia" in profile
    assert "count: 1" in profile
    assert "worker-training:" not in profile
    assert "worker-alerts:" not in profile


def test_gpu_readiness_does_not_start_production_services():
    script = (ROOT / "scripts" / "gpu-readiness.sh").read_text()

    assert "docker run --rm --gpus all" in script
    assert "torch.cuda.is_available()" in script
    assert '"CUDAExecutionProvider"' in script
    assert "docker compose down" not in script
    assert " up -d" not in script


def test_migration_runbook_has_safety_and_rollback_gates():
    runbook = (ROOT / "docs" / "GEX44_MIGRATION_RUNBOOK.md").read_text()

    for required in (
        "action-time approval",
        "off-host PostgreSQL dump",
        "temporary private hostname",
        "Public API",
        "silently falls back to CPU",
        "at least 20% sustained headroom",
        "## Rollback",
    ):
        assert required in runbook
