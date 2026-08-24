from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_gpu_override_is_isolated_to_inference():
    profile = (ROOT / "docker-compose.gpu.yml").read_text()

    assert "vivoguard/worker:cuda" in profile
    assert "GPU_BACKEND: cuda" in profile
    assert 'USE_GPU: "true"' in profile
    assert "INFERENCE_MAX_BATCH_SIZE" in profile
    assert "driver: nvidia" in profile
    assert "count: 1" in profile
    assert "worker-training:" not in profile
    assert "worker-alerts:" not in profile


def test_batch_shadow_is_opt_in_and_cannot_emit_authoritative_alerts():
    base = (ROOT / "docker-compose.yml").read_text()
    profile = (ROOT / "docker-compose.gpu.yml").read_text()
    coordinator = (
        ROOT / "backend" / "app" / "ai" / "batch_coordinator.py"
    ).read_text()

    assert 'profiles: ["gpu-batch-shadow"]' in base
    assert 'INFERENCE_BATCH_SHADOW_ENABLED: "true"' in base
    assert "worker-inference-batch-shadow:" in profile
    assert '"authoritative": False' in coordinator
    assert "authoritative batch mode is not implemented" in coordinator


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
        "gpu_concurrency_benchmark.py",
        "p95 latency per frame",
        "INFERENCE_SHARD_COUNT",
        "## Rollback",
    ):
        assert required in runbook


def test_gpu_benchmark_is_packaged_and_rejects_cpu_fallback():
    dockerfile = (ROOT / "backend" / "Dockerfile.worker").read_text()
    benchmark = (
        ROOT / "backend" / "scripts" / "gpu_concurrency_benchmark.py"
    ).read_text()

    assert "COPY scripts ./scripts" in dockerfile
    assert 'env.backend != "cuda"' in benchmark
    assert "torch.cuda.synchronize()" in benchmark
    assert '"recommended_batch_size"' in benchmark
