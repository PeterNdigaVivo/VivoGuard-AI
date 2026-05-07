# Hardware sizing guide

Rough sizing based on YOLOv8n (640px) at 5 FPS per camera.

| Cameras | CPU            | RAM   | GPU                    | Storage (30d retention 2Mbps) |
|---------|----------------|-------|------------------------|-------------------------------|
|   4–8   | 4-core         |  8 GB | optional (CPU OK)      |  1.5 TB                       |
|  16     | 8-core         | 16 GB | RTX 3060 (12 GB VRAM)  |  3 TB                         |
|  32     | 16-core        | 32 GB | RTX 3080 (10 GB VRAM)  |  6 TB                         |
|  64     | 16-core        | 64 GB | RTX 4090 (24 GB VRAM)  | 12 TB                         |
| 128     | 32-core (2x)   | 128 GB| 2× RTX 4090            | 24 TB                         |

WAN streams: budget **~5–8 Mbps per HD camera** of internet uplink. Use
substreams (640×360, 5 FPS) for the inference path to cut bandwidth ~10×.
