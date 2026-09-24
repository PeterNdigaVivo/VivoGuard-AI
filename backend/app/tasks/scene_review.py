"""Open-ended scene review — the detector that needs no rule.

Every other detector in the system checks a condition someone wrote in
advance: is the counter empty, did a track cross this line, has this
object been still for N seconds. That works, and it is exactly why a
contractor carrying a ladder across the shop floor produced nothing —
no rule describes a ladder, and no list of rules ever finishes.

This task inverts it. It shows the VLM a frame and asks an open
question: is anything here worth a manager's attention? One prompt
covers ladders, cable work, cleaning during trade, children climbing
displays, someone idle for a long stretch, and the ones nobody has
thought of yet.

Two properties worth preserving if this is ever refactored:

  * The frame sent to the model IS the frame saved as the snapshot.
    The alert's wording and its picture describe the same instant by
    construction, rather than being captured separately and hoping
    they agree.
  * It runs on the `alerts` queue. That is the only worker with the
    host.docker.internal mapping needed to reach Ollama, and keeping
    VLM work off `inference` protects the camera pipeline.

Shadow mode (the default) runs everything except the alert: verdicts
go to the log and to a metric, so the false-positive rate can be read
before an operator ever sees one.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

# The model is asked to answer with this exact token when a frame is
# unremarkable. Cheap to produce (one token) and unambiguous to parse,
# which matters because the boring answer is the overwhelmingly common
# one — a sweep that mostly returns NONE is a sweep that stays fast.
_NONE = "NONE"

_CURSOR_KEY = "vg:scene_review:cursor"

_SYSTEM = (
    "You are reviewing a single still frame from a CCTV camera in a Vivo "
    "fashion retail store in East Africa. You are advising the store "
    "manager, not writing a report. Be literal: describe only what is "
    "visibly in the frame. Never guess at intent, identity or motive."
)

# A JSON boolean rather than a magic token. The first version asked for
# the word NONE and got prose back a quarter of the time — the model
# would decide a frame was fine and then write "The scene is ordinary
# retail activity", which any token check reads as a finding. It also
# echoed the instruction ("ONE plain sentence: ...") into its answer.
# A boolean field cannot do either.
#
# The positive list is narrow on purpose. Left open, the model reported
# every person standing near a till. These are the categories that
# actually produced true hits in shadow — ladder, child on a counter,
# cleaning equipment mid-trade, someone in a staff-only area.
_QUESTION = (
    "Does this frame show something the store manager should act on?\n\n"
    "Reply with JSON only, no other text:\n"
    '{"noteworthy": true or false, "description": "one plain sentence"}\n\n'
    "Use false for ordinary retail activity — customers browsing, "
    "waiting or paying; staff serving, tidying, working at the till or "
    "using the till computer or a phone at the counter; an empty aisle; "
    "a closed and empty store. Leave description empty when false.\n\n"
    "Use true for any of these:\n"
    "- a ladder, tools, cables being worked on, or any maintenance or "
    "repair work\n"
    "- cleaning equipment in use while the store is trading\n"
    "- a person in a staff-only, stock or back-of-house area\n"
    "- a child climbing on counters, shelves, displays or furniture\n"
    "- boxes, stock or equipment blocking a walkway, doorway or exit\n"
    "- an external door, shutter or gate standing open while the store "
    "is closed\n"
    "- a spill, breakage, or anything on the floor that could trip "
    "someone\n"
    "- a person carrying large boxes or equipment across the shop floor\n"
    "- a person sitting or lying on the floor, or on display furniture\n"
    "- an animal inside the store\n"
    "- a group standing close together who are not shopping\n"
    "- anything else clearly out of place for a clothing shop\n\n"
    "Describe only what is visible. Never infer mood, health, intent or "
    "identity — report what a person is doing, not how they seem to feel."
)

# When the model ignores the schema and writes prose, these are the
# phrases it uses to say "nothing here" — observed verbatim in shadow.
# Cheaper and more honest than letting a paragraph become an alert.
_ALL_CLEAR = (
    "ordinary retail activity", "operating normally", "trading as usual",
    "trading normally", "nothing unusual", "no signs of",
    "no unusual activity", "require immediate attention",
)


def _context_line(store, camera, is_open: bool, local_now) -> str:
    """Ground the model in where and when this frame is from.

    The model reads the burned-in timestamp and camera label off the
    frame anyway, so withholding context does not hide it — it just
    invites the model to guess. A frame at 22:10 in a closed store
    means something very different from the same frame at 14:00.
    """
    where = ", ".join(x for x in (
        getattr(store, "name", None), getattr(camera, "name", None)) if x)
    state = "OPEN and trading" if is_open else "CLOSED to customers"
    return (f"Location: {where or 'unknown'}. "
            f"Local time: {local_now.strftime('%A %H:%M')}. "
            f"The store is currently {state}.")


def _ask(jpeg: bytes, context: str) -> str | None:
    """One VLM call. Returns the raw reply, or None if it failed.

    Reuses alert_verifier's provider plumbing rather than opening a
    second HTTP path to the same Ollama instance.
    """
    from app.services.alert_verifier import _post_json

    base = str(getattr(settings, "verifier_ollama_url",
                       "http://host.docker.internal:11434")).rstrip("/")
    data = _post_json(
        base + "/api/chat",
        headers={},
        payload={
            "model": settings.scene_review_model,
            "stream": False,
            # Constrained decoding: Ollama guarantees syntactically valid
            # JSON, so the only failure left is a wrong judgement rather
            # than an unparseable one.
            "format": "json",
            "options": {"num_predict": settings.scene_review_max_tokens,
                        # Near-greedy: this is a judgement, not prose.
                        # Sampling variance here shows up as a camera
                        # that flags on one sweep and not the next.
                        "temperature": 0.1},
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user",
                 "content": context + "\n\n" + _QUESTION,
                 "images": [base64.b64encode(jpeg).decode()]},
            ],
        },
        timeout=settings.scene_review_timeout_seconds)
    return str((data.get("message") or {}).get("content") or "").strip() or None


def is_noteworthy(reply: str | None) -> bool:
    """Prose fallback: True when the text reports something.

    Only reached when the JSON verdict is unusable. Tolerates the shapes
    the model actually produces — "NONE", "NONE.", "none", "NONE - all
    clear" — and treats the all-clear phrases it writes when it ignores
    the schema as negatives too, since a paragraph saying "nothing here"
    must not become an alert.
    """
    if not reply:
        return False
    text = reply.strip()
    if text.lstrip("*#- ").upper().startswith(_NONE):
        return False
    low = text.lower()
    return not any(phrase in low for phrase in _ALL_CLEAR)


def parse_verdict(reply: str | None) -> str | None:
    """The description when the frame is worth reporting, else None.

    Prefers the JSON contract; falls back to reading the prose when the
    model returns something else. A `noteworthy: true` with no
    description is treated as nothing — an alert an operator cannot act
    on is worse than no alert.
    """
    import json

    if not reply:
        return None
    text = reply.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "noteworthy" in data:
            flag = data.get("noteworthy")
            if isinstance(flag, str):
                flag = flag.strip().lower() in ("true", "yes", "1")
            desc = " ".join(str(data.get("description") or "").split())
            if not flag or not desc:
                return None
            # The model occasionally sets the flag and then describes an
            # ordinary scene. Trust the words over the boolean.
            return None if not is_noteworthy(desc) else desc
    except (ValueError, TypeError):
        pass
    return " ".join(text.split()) if is_noteworthy(text) else None


def _save_frame(camera_id: int, jpeg: bytes) -> str | None:
    """Persist the exact bytes shown to the model, so the alert's
    picture is the evidence for its wording. Best-effort: a snapshot
    that fails to write must never suppress the alert."""
    try:
        root = (Path(settings.recordings_dir) / "snapshots"
                / datetime.now().strftime("%Y-%m-%d"))
        root.mkdir(parents=True, exist_ok=True)
        path = root / ("scene_review_cam%d_%s.jpg"
                       % (camera_id, datetime.now().strftime("%H%M%S_%f")))
        path.write_bytes(jpeg)
        return str(path)
    except Exception as e:
        log.warning("scene_review: snapshot save failed cam=%s: %s",
                    camera_id, e)
        return None


def _due_cameras(db, r, limit: int):
    """Next `limit` cameras, walking the fleet round-robin.

    A Redis cursor rather than a random sample: random sampling revisits
    some cameras often and starves others, and a fleet-wide burst every
    sweep would contend with inference for the same CPU.
    """
    from app.models import Camera

    cams = (db.query(Camera)
              .filter(Camera.ai_enabled.is_(True),
                      Camera.deleted_at.is_(None))
              .order_by(Camera.id.asc())
              .all())
    if not cams:
        return []
    try:
        start = int(r.get(_CURSOR_KEY) or 0)
    except Exception:
        start = 0
    start %= len(cams)
    picked = [cams[(start + i) % len(cams)] for i in range(min(limit, len(cams)))]
    try:
        r.set(_CURSOR_KEY, (start + len(picked)) % len(cams))
    except Exception:
        pass
    return picked


@celery_app.task(name="scene_review.sweep", ignore_result=True)
def scene_review_sweep() -> None:
    """Review a slice of the fleet. One VLM call per camera, at most."""
    if not settings.scene_review_enabled:
        return

    from app.analytics import recorder
    from app.database import SessionLocal
    from app.models import Store
    from app.stream.frame_buffer import FrameBuffer
    from app.tasks.alerting import _redis, _within_operating_hours
    from app.utils.business_hours import _store_local_now

    shadow = bool(settings.scene_review_shadow_mode)
    fb = FrameBuffer()
    r = _redis()
    looked = flagged = 0

    with SessionLocal() as db:
        for camera in _due_cameras(db, r, settings.scene_review_cameras_per_sweep):
            try:
                store = (db.get(Store, camera.store_id)
                         if camera.store_id else None)
                if settings.scene_review_trading_hours_only \
                        and not _within_operating_hours(store):
                    continue
                # prefer_overlay=False: the detector overlays (numbered
                # boxes, flow lines) are our own annotations. Showing
                # them to the model invites it to describe our drawing
                # instead of the shop.
                jpeg = fb.latest_jpeg(int(camera.id), prefer_overlay=False)
                if not jpeg:
                    continue

                local_now = _store_local_now(store)
                reply = _ask(jpeg, _context_line(
                    store, camera, _within_operating_hours(store), local_now))
                looked += 1

                recorder.record(db, "scene_review_checked", 1.0,
                                camera_id=camera.id, store_id=camera.store_id,
                                aggregator="sum")
                note = parse_verdict(reply)
                if not note:
                    continue

                flagged += 1
                note = note[:500]
                log.info("scene_review%s cam=%s store=%s: %s",
                         " [shadow]" if shadow else "", camera.id,
                         camera.store_id, note)
                recorder.record(db, "scene_review_flagged", 1.0,
                                camera_id=camera.id, store_id=camera.store_id,
                                aggregator="sum")
                db.commit()

                if shadow:
                    continue
                # One alert per camera per dedup window — a ladder that
                # stays up for an hour is one situation, not twelve.
                if not r.set(f"vg:scene_review:fired:{camera.id}", "1",
                             nx=True, ex=settings.scene_review_dedup_seconds):
                    continue

                from app.tasks.alerting import _create_info_alert
                _create_info_alert(
                    db, camera_id=int(camera.id), zone_id=None,
                    store_id=camera.store_id,
                    detection_type="scene_review", cls="unusual_activity",
                    extra={"rule": "unusual_activity", "priority": "high",
                           "description": note,
                           "model": settings.scene_review_model,
                           "store_id": camera.store_id,
                           "camera_name": camera.name},
                    # The frame the model actually described.
                    thumbnail_path=_save_frame(int(camera.id), jpeg),
                )
                db.commit()
            except Exception:
                db.rollback()
                log.exception("scene_review: camera=%s failed", camera.id)

    if looked:
        log.info("scene_review sweep%s: %d looked, %d flagged",
                 " [shadow]" if shadow else "", looked, flagged)
