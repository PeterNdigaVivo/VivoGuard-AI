#!/usr/bin/env bash
# Read-only host checks plus an isolated container smoke test. This does not
# start, stop or replace any VivoGuard production service.
set -euo pipefail

cd "$(dirname "$0")/.."

command -v docker >/dev/null || { echo "docker not found"; exit 1; }
docker compose version >/dev/null || { echo "docker compose plugin not found"; exit 1; }
command -v nvidia-smi >/dev/null || { echo "NVIDIA driver / nvidia-smi not found"; exit 1; }

echo "Checking host GPU"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader

echo "Checking Docker NVIDIA runtime"
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 \
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader

echo "Validating GPU Compose profile"
docker compose -f docker-compose.yml -f docker-compose.gpu.yml config --quiet

echo "Building isolated CUDA inference image"
docker compose -f docker-compose.yml -f docker-compose.gpu.yml build worker-inference

echo "Checking CUDA, TensorRT and ONNX providers inside the worker image"
docker compose -f docker-compose.yml -f docker-compose.gpu.yml \
  run --rm --no-deps worker-inference python - <<'PY'
import onnxruntime
import tensorrt
import torch

assert torch.cuda.is_available(), "PyTorch cannot see the NVIDIA GPU"
providers = onnxruntime.get_available_providers()
assert "CUDAExecutionProvider" in providers, providers
print({
    "gpu": torch.cuda.get_device_name(0),
    "cuda": torch.version.cuda,
    "tensorrt": tensorrt.__version__,
    "onnx_providers": providers,
})
PY

echo "GPU readiness checks passed. No production service was changed."
