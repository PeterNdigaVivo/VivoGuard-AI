#!/usr/bin/env bash
# Pre-download the YOLOv8 base weights so the first inference run is fast.
# Weights are placed in ./data/models/ (mounted at /data/models/ in containers).
set -euo pipefail

MODELS_DIR="${MODELS_DIR:-./data/models}"
mkdir -p "$MODELS_DIR"

BASE="https://github.com/ultralytics/assets/releases/download/v8.3.0"
for w in yolov8n.pt yolov8s.pt yolov8m.pt yolov8l.pt yolov8x.pt; do
  if [ ! -f "$MODELS_DIR/$w" ]; then
    echo "Downloading $w..."
    curl -L -o "$MODELS_DIR/$w" "$BASE/$w"
  else
    echo "$w already present, skipping."
  fi
done
echo "Done. Weights live in $MODELS_DIR"
