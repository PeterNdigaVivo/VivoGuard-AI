"""AI-drafted store floor plans (Heatmap Insights page).

generate_floor_plan() shows the configured Anthropic model one live
frame per camera (when the streamer has one in Redis) plus the camera
names and their zones' semantic tags, and asks for a SCHEMATIC layout:
labelled area blocks plus a coverage rectangle per camera, all in
normalised [0..1] plan coordinates. The JSON reply is rendered to a
floor-plan PNG with PIL and the camera rectangles become
floor_placement_json values, so the store gets a plan AND pre-placed
cameras in one shot. The draft is topological, not to scale — the
operator nudges blocks/placements afterwards and can replace the image
with a real drawing at any time without losing placements.

Fail behaviour: RuntimeError("anthropic_api_key not configured") when
the key is missing (endpoint maps it to 503); ValueError on an
unusable model reply (endpoint maps to 502). Never writes anything on
failure.
"""
from __future__ import annotations

import base64
import io
import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Camera, Store, Zone

log = logging.getLogger(__name__)

MAX_FRAMES = 8          # cost cap: one vision call per store, few cents
FRAME_WIDTH = 512
PLAN_W, PLAN_H = 1600, 1200

_SYSTEM = """You are drafting a SCHEMATIC top-down floor plan of one
retail clothing store from its CCTV cameras. You are given each
camera's name, the semantic tags of the detection zones drawn on it
(counter, entry_exit, staff_zone, aisle, ...), and, for some cameras,
a current frame from that camera.

Reply with ONE JSON object and nothing else:
{
  "areas": [
    {"label": "TILLS", "kind": "area", "x": 0.7, "y": 0.4, "w": 0.25, "h": 0.3}
  ],
  "cameras": {
    "<camera_id>": {"x": 0.65, "y": 0.35, "w": 0.3, "h": 0.4}
  }
}

Rules:
- Coordinates are normalised [0..1] over the whole plan; x,y is the
  top-left corner of each rectangle.
- "kind" is "area" (a named region: TILLS, FITTING ROOMS, STOCKROOM),
  "fixture" (shelving/display blocks, label optional), or "entrance".
- Put the entrance at the BOTTOM edge. Place the till/counter area
  where the frames and zone tags suggest. Areas must not overlap.
- Include EVERY camera id you were given in "cameras", covering the
  region that camera most plausibly watches (rectangles may overlap).
- 4 to 10 areas. Uppercase short labels. This is a schematic — commit
  to a plausible layout rather than refusing."""


def _downscale_jpeg(raw: bytes) -> str | None:
    """Downscale a camera frame for the vision call; base64 JPEG."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        img.load()
        if img.width > FRAME_WIDTH:
            img = img.resize(
                (FRAME_WIDTH, max(1, int(img.height * FRAME_WIDTH / img.width))))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _ask_model(user_text: str, images_b64: list[str]) -> dict:
    import anthropic
    api_key = str(getattr(settings, "anthropic_api_key", "") or "")
    if not api_key:
        raise RuntimeError("anthropic_api_key not configured")
    client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
    content: list[dict] = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg",
                    "data": b64}}
        for b64 in images_b64
    ]
    content.append({"type": "text", "text": user_text})
    msg = client.messages.create(
        model=str(getattr(settings, "verifier_model", "claude-sonnet-4-6")),
        max_tokens=2000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(getattr(b, "text", "") or "" for b in (msg.content or []))
    from app.services.alert_verifier import _extract_json
    return _extract_json(text)


def _clamp_rect(r: dict) -> dict | None:
    try:
        x = min(1.0, max(0.0, float(r["x"])))
        y = min(1.0, max(0.0, float(r["y"])))
        w = min(1.0 - x, max(0.01, float(r["w"])))
        h = min(1.0 - y, max(0.01, float(r["h"])))
        return {"x": round(x, 4), "y": round(y, 4),
                "w": round(w, 4), "h": round(h, 4)}
    except Exception:
        return None


def _render_plan(areas: list[dict]) -> bytes:
    """Draw the schematic: parchment ground, outlined areas with
    labels, filled fixtures, a dark entrance strip."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (PLAN_W, PLAN_H), (250, 247, 242))
    d = ImageDraw.Draw(img)
    d.rectangle([3, 3, PLAN_W - 4, PLAN_H - 4], outline=(168, 162, 158),
                width=5)
    try:
        font = ImageFont.load_default(size=30)
        small = ImageFont.load_default(size=22)
    except TypeError:          # older Pillow: no size argument
        font = small = ImageFont.load_default()

    for a in areas:
        r = _clamp_rect(a)
        if r is None:
            continue
        x0, y0 = int(r["x"] * PLAN_W), int(r["y"] * PLAN_H)
        x1, y1 = int((r["x"] + r["w"]) * PLAN_W), int((r["y"] + r["h"]) * PLAN_H)
        kind = str(a.get("kind") or "area")
        label = str(a.get("label") or "").upper()[:28]
        if kind == "fixture":
            d.rectangle([x0, y0, x1, y1], fill=(231, 229, 228),
                        outline=(214, 211, 209), width=2)
            f = small
        elif kind == "entrance":
            d.rectangle([x0, y0, x1, y1], fill=(87, 83, 78))
            f = small
        else:
            d.rectangle([x0, y0, x1, y1], outline=(168, 162, 158), width=4)
            f = font
        if label:
            colour = (255, 255, 255) if kind == "entrance" else (120, 113, 108)
            bb = d.textbbox((0, 0), label, font=f)
            d.text((max(x0 + 8, (x0 + x1 - (bb[2] - bb[0])) // 2),
                    max(y0 + 6, (y0 + y1 - (bb[3] - bb[1])) // 2)),
                   label, fill=colour, font=f)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def generate_floor_plan(db: Session, store: Store
                        ) -> tuple[bytes, dict[int, dict]]:
    """Returns (plan_png_bytes, {camera_id: placement}). Raises
    RuntimeError (no key) or ValueError (unusable reply)."""
    cams = (db.query(Camera)
              .filter(Camera.store_id == store.id)
              .order_by(Camera.id).all())
    if not cams:
        raise ValueError("store has no cameras to draft a plan from")

    zones_by_cam: dict[int, list[str]] = {}
    for z in (db.query(Zone)
                .filter(Zone.camera_id.in_([c.id for c in cams])).all()):
        tags = list(z.detection_types_json or [])
        name = getattr(z, "name", None)
        desc = f"{name} [{', '.join(tags)}]" if name else ", ".join(tags)
        zones_by_cam.setdefault(z.camera_id, []).append(desc)

    images: list[str] = []
    lines: list[str] = [f"Store: {store.name}", "Cameras:"]
    try:
        from app.stream.frame_buffer import FrameBuffer
        fb = FrameBuffer()
    except Exception:
        fb = None
    for c in cams:
        frame_note = "no frame"
        if fb is not None and len(images) < MAX_FRAMES:
            try:
                raw = fb.latest_jpeg(c.id, prefer_overlay=False)
            except Exception:
                raw = None
            if raw:
                b64 = _downscale_jpeg(raw)
                if b64:
                    images.append(b64)
                    frame_note = f"frame = image {len(images)}"
        zdesc = "; ".join(zones_by_cam.get(c.id, [])) or "no zones drawn"
        lines.append(f"- id {c.id}: \"{c.name}\" — zones: {zdesc} ({frame_note})")
    lines.append("Draft the floor plan JSON now.")

    reply = _ask_model("\n".join(lines), images)

    areas = reply.get("areas")
    cam_rects = reply.get("cameras")
    if not isinstance(areas, list) or not areas or not isinstance(cam_rects, dict):
        raise ValueError("model reply missing areas/cameras")

    valid_ids = {c.id for c in cams}
    placements: dict[int, dict] = {}
    for key, rect in cam_rects.items():
        try:
            cid = int(key)
        except (TypeError, ValueError):
            continue
        if cid not in valid_ids:
            continue
        r = _clamp_rect(rect if isinstance(rect, dict) else {})
        if r is not None:
            placements[cid] = {**r, "rot": 0}
    if not placements:
        raise ValueError("model reply placed no known cameras")

    png = _render_plan([a for a in areas if isinstance(a, dict)])
    log.info("floor-plan AI draft: store=%s areas=%d cameras_placed=%d "
             "frames_sent=%d", store.id, len(areas), len(placements),
             len(images))
    return png, placements
