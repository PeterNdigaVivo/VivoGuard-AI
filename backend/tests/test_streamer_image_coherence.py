from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_streamer_image_copies_backend_and_streamer_from_one_checkout() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "streamer" / "Dockerfile").read_text(encoding="utf-8")
    service = compose.split("  streamer:", 1)[1].split("\n  frontend:", 1)[0]

    assert "context: ." in service
    assert "dockerfile: streamer/Dockerfile" in service
    assert "COPY backend/app ./app" in dockerfile
    assert "COPY streamer/streamer ./streamer" in dockerfile
