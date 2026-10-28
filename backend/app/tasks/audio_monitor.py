"""Audio distress monitoring — raised voices at counters and on the floor.

Why audio, and why only this
----------------------------
Visual detection cannot see a confrontation. A shouting match at a till
looks, to YOLO, like two people standing near each other. Audio closes
that gap, and it is the one audio signal this hardware can actually
carry.

Measured on the fleet (2026-10-23), the microphones are 8 kHz mono G.711
and roll off hard below ~200 Hz:

    Greenspan cam 65     50-100 Hz   0.08%
                        100-200 Hz   1.40%
                        200-300 Hz  18.24%     <- cliff edge
                        500-1000 Hz 23.82%

So roller-shutter motor hum (50-200 Hz) is NOT detectable on this chain —
that idea is dead until someone moves a microphone. Speech, however, sits
at 200-3400 Hz, exactly where these mics perform best. Hence distress
detection rather than shutter detection.

Privacy posture — deliberately narrow
-------------------------------------
Voice is personal data under Kenya's DPA 2019, Uganda's DPPA 2019 and
Rwanda's Law 058/2021, and audio is treated more strictly than video in
all three. So this module:

  * never runs a rolling recorder — PCM is piped from ffmpeg only during
    the sweep sample, reduced to a handful of floats, and the buffer is
    dropped the moment the camera does not alert;
  * never transcribes, and holds no speech-recognition code path;
  * stores only derived numbers (loudness ratio, band shares) on the
    event.

It answers "someone is shouting", never "someone said X". That boundary
is what makes it defensible, so keep it.

The one thing that IS written (2026-10-28)
------------------------------------------
When — and only when — a sample crosses the threshold and fires an
alert, that same 6-second buffer is encoded to a small AAC file and
attached to the alert. Operators could see the still frame but not hear
the sound, so every audio alert ended in "open the live camera", by
which time the moment had passed; a cheer and a confrontation are
indistinguishable from loudness and flatness alone.

Note what this is NOT, because the distinction is the whole defence:

  * it is the audio that already triggered an alert, not a buffer kept
    on the chance something happens;
  * it is 6 seconds, not 30 — nothing is captured after detection, so
    this is the incident rather than its aftermath;
  * cameras that never fire leave nothing behind at all;
  * it still is not transcribed, and it ages out on a 48h clock
    (`audio_clip_retention_hours`) regardless of alert status.

`audio_clip_enabled = False` restores the previous measure-and-discard
behaviour without a code change. If you turn it on, the staff notice and
signage obligations under the three DPAs above are yours to meet — this
module stores voice now, and the alert card says so.

Only ~22 of 97 reachable cameras have a microphone at all, and every one
observed is NVR channel 1, so coverage is roughly one camera per store.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

SAMPLE_RATE = 8000                  # G.711 — what the hardware gives us
_CAPABLE_KEY = "vg:audio:capable"   # redis set of camera ids with a mic
_BASELINE_KEY = "vg:audio:baseline:{camera_id}"

# Speech occupies roughly 200-3400 Hz. A bang, a dropped rail or a trolley
# is broadband and does not concentrate here, which is how we tell a
# shout from a clatter without any speech recognition.
_SPEECH_LO, _SPEECH_HI = 200.0, 3400.0


def _pcm(url: str, seconds: int) -> bytes:
    """Pull mono PCM from an RTSP stream. Never touches disk."""
    cmd = ["ffmpeg", "-v", "quiet", "-rtsp_transport", "tcp", "-i", url,
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
           "-f", "s16le", "-t", str(seconds), "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=seconds + 20)
        return p.stdout or b""
    except subprocess.TimeoutExpired:
        return b""
    except Exception as exc:                                # noqa: BLE001
        log.debug("audio: ffmpeg failed (%s)", exc)
        return b""


def _save_audio_sample(camera_id: int, pcm: bytes) -> str | None:
    """Persist the sample that fired an alert as a small AAC file.

    This is the ONLY place this module writes audio, and it writes only
    a buffer that has already crossed the alert threshold. Callers must
    not reach for it speculatively — see the module docstring.

    6 seconds of 8 kHz mono at 32 kbps is roughly 24 KB, so a fleet-wide
    day of alerts costs a few megabytes and the 48h retention window is
    free in practice.

    Best-effort by design: a sample that fails to encode must never cost
    us the alert, because the alert is still correct without it.
    """
    if not bool(getattr(settings, "audio_clip_enabled", True)) or not pcm:
        return None
    path = None
    try:
        root = (Path(settings.recordings_dir) / "audio_clips"
                / datetime.now().strftime("%Y-%m-%d"))
        root.mkdir(parents=True, exist_ok=True)
        path = root / ("audio_cam%d_%s.m4a"
                       % (camera_id, datetime.now().strftime("%H%M%S_%f")))
        # Re-encoded rather than dumped as WAV: raw s16le would be ~94 KB
        # for the same 6 seconds and no browser plays it from a blob
        # without a container.
        res = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "quiet", "-y",
             "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
             "-i", "pipe:0",
             "-c:a", "aac", "-b:a", "32k",
             "-movflags", "+faststart", str(path)],
            input=pcm, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=60)
        if res.returncode == 0 and path.exists() and path.stat().st_size > 0:
            return str(path)
        path.unlink(missing_ok=True)
    except Exception as e:                                  # noqa: BLE001
        log.warning("audio: sample save failed cam=%s: %s", camera_id, e)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except Exception:                               # noqa: BLE001
                pass
    return None


def analyse(pcm: bytes) -> dict[str, float] | None:
    """Reduce raw PCM to a few numbers. Returns None if there is nothing
    usable. The input buffer is not retained."""
    import numpy as np

    if len(pcm) < SAMPLE_RATE:          # under half a second
        return None
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if x.size == 0:
        return None

    rms = float(np.sqrt((x ** 2).mean()))
    peak = float(np.abs(x).max())

    n = 1024
    frames = x.size // n
    if frames < 1:
        return None
    win = np.hanning(n)
    spec = np.zeros(n // 2 + 1, dtype=np.float32)
    for i in range(frames):
        spec += np.abs(np.fft.rfft(x[i * n:(i + 1) * n] * win))
    spec /= frames
    freqs = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
    total = float(spec.sum()) or 1.0
    speech = float(spec[(freqs >= _SPEECH_LO) & (freqs < _SPEECH_HI)].sum())

    # Spectral flatness = geometric mean / arithmetic mean, over the band
    # the hardware actually passes. Near 0 for harmonic sound (a voice),
    # near 1 for noise-like sound (a trolley, a dropped hanger rail, a
    # shutter).
    #
    # This carries the discrimination, NOT speech_share. Measured on live
    # cameras, speech_share sits at 76-96% even in an empty shop, because
    # the mic is band-limited to ~200-4000 Hz and therefore puts almost
    # all of its energy inside the "speech" band whatever the source.
    # Flatness is the only cheap feature here that still separates a
    # raised voice from a loud bang.
    band = spec[(freqs >= _SPEECH_LO) & (freqs < _SPEECH_HI)]
    band = band[band > 0]
    if band.size:
        flatness = float(np.exp(np.log(band).mean()) / band.mean())
    else:
        flatness = 1.0

    return {"rms": rms, "peak": peak,
            "speech_share": speech / total,
            "flatness": flatness,
            "seconds": x.size / float(SAMPLE_RATE)}


def is_raised_voice(feat: dict[str, float], baseline_rms: float) -> tuple[bool, float]:
    """True when this sample is much louder than the room's norm AND
    harmonic enough to be a voice rather than a crash.

    All three conditions are required. Loudness alone fires on a shutter,
    a trolley or a dropped hanger rail. Speech-band share turned out to
    be nearly useless on this hardware (76-96% even in an empty shop,
    because the mic only passes ~200-4000 Hz in the first place), so it
    is kept only as a weak floor. Flatness does the real work: a shout is
    harmonic and scores low, an impact is noise-like and scores high.
    """
    ratio = feat["rms"] / baseline_rms if baseline_rms > 1e-6 else 0.0
    loud = ratio >= float(getattr(settings, "audio_rms_ratio", 3.0))
    speechy = feat["speech_share"] >= float(
        getattr(settings, "audio_speech_share_min", 0.45))
    harmonic = feat.get("flatness", 1.0) <= float(
        getattr(settings, "audio_flatness_max", 0.45))
    return (loud and speechy and harmonic), ratio


def _baseline(r: Any, camera_id: int, rms: float, *, update: bool) -> float:
    """Rolling per-camera loudness norm. A quiet shop and a mall unit next
    to a food court have very different floors, so the threshold has to be
    relative, not absolute.

    Only quiet samples update it — otherwise a long argument would raise
    the baseline until it stopped being detectable.
    """
    key = _BASELINE_KEY.format(camera_id=camera_id)
    try:
        prev = r.get(key)
        prev_f = float(prev) if prev else None
    except Exception:                                       # noqa: BLE001
        prev_f = None
    if prev_f is None:
        try:
            r.set(key, repr(rms), ex=86400)
        except Exception:                                   # noqa: BLE001
            pass
        return rms
    if update:
        blended = 0.9 * prev_f + 0.1 * rms
        try:
            r.set(key, repr(blended), ex=86400)
        except Exception:                                   # noqa: BLE001
            pass
    return prev_f


def capable_camera_ids(db, r) -> list[int]:
    """Cameras whose stream carries an audio track.

    Probing costs an ffprobe per camera, so the answer is cached for a
    day. Mic fitment is a hardware fact; it does not change between
    sweeps.
    """
    try:
        cached = r.smembers(_CAPABLE_KEY)
        if cached:
            return sorted(int(c) for c in cached)
    except Exception:                                       # noqa: BLE001
        pass

    import json
    from sqlalchemy import text
    from app.models import Camera
    from app.utils.crypto import decrypt
    from app.utils.network import build_rtsp_url

    # Probing costs an ffprobe per camera (up to 20s each). Probing all
    # 113 inline would block the alerts worker for half an hour and let
    # 2-minute beat runs pile up on top of each other, so the discovery
    # is spread across sweeps: a cursor walks the fleet a slice at a
    # time and the set fills in over a few minutes.
    probe_limit = int(getattr(settings, "audio_probe_per_sweep", 12))
    found: list[int] = []
    rows = db.execute(text("""
        SELECT c.id FROM cameras c
        WHERE NOT c.is_deleted AND c.ai_enabled ORDER BY c.id""")).scalars().all()
    try:
        cursor = int(r.get("vg:audio:probe_cursor") or 0)
    except Exception:                                       # noqa: BLE001
        cursor = 0
    window = rows[cursor:cursor + probe_limit]
    next_cursor = cursor + probe_limit
    if next_cursor >= len(rows):
        next_cursor = 0                      # wrapped: a full pass is done
    try:
        r.set("vg:audio:probe_cursor", str(next_cursor), ex=86400)
    except Exception:                                       # noqa: BLE001
        pass
    for cid in window:
        cam = db.query(Camera).filter(Camera.id == cid).first()
        if not cam:
            continue
        try:
            pw = decrypt(cam.password_encrypted)
        except Exception:                                   # noqa: BLE001
            continue
        url = build_rtsp_url(brand=cam.brand or "hikvision", host=cam.host,
                             port=cam.rtsp_port or 554, username=cam.username,
                             password=pw, channel=cam.channel_number or 1,
                             subtype=0)
        try:
            p = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_streams", "-rtsp_transport", "tcp", url],
                capture_output=True, text=True, timeout=20)
            if p.returncode == 0 and p.stdout.strip():
                d = json.loads(p.stdout)
                if any(s.get("codec_type") == "audio" for s in d.get("streams", [])):
                    found.append(int(cid))
        except Exception:                                   # noqa: BLE001
            continue
    try:
        if found:
            r.sadd(_CAPABLE_KEY, *found)
            # Only start the day-long expiry once a full pass has wrapped,
            # otherwise the set would expire mid-discovery and restart.
            if next_cursor == 0:
                r.expire(_CAPABLE_KEY, 86400)
        known = sorted(int(c) for c in (r.smembers(_CAPABLE_KEY) or []))
    except Exception:                                       # noqa: BLE001
        known = found
    log.info("audio: probed %d cameras (cursor %d/%d), %d with a microphone "
             "known so far", len(window), cursor, len(rows), len(known))
    return known


_VLM_SYSTEM = (
    "You describe retail CCTV frames for a control-room operator in one "
    "plain sentence. State only what is visibly happening: how many people, "
    "where they are, what they appear to be doing. Do not speculate about "
    "motives, emotions or what anyone is saying. If the frame shows nothing "
    "but an empty shop, say so plainly."
)


def describe_scene(camera_id: int, camera_name: str, store_name: str | None,
                   ratio: float) -> tuple[str | None, bytes | None]:
    """Ask the VLM what the camera SEES, to pair with what the mic HEARD.

    The microphone can only report that something loud and voice-like
    happened; it cannot say who or what. On its own that leaves an
    operator opening a live view to find out. One VLM call on the frame
    captured at that moment turns "Audio Distress" into something
    actionable before anyone clicks.

    Note what is and is not analysed here: the VIDEO frame, which is
    already covered by existing CCTV use. The audio itself is never sent
    anywhere, never stored and never transcribed - only the loudness
    figure reaches this function.

    Best effort. A VLM that is slow, down or unhelpful must never cost us
    the alert, so every failure returns None and the alert still fires
    with its measured numbers.
    """
    import base64
    try:
        from app.services.alert_verifier import _post_json
        from app.stream.frame_buffer import FrameBuffer

        jpeg = FrameBuffer().latest_jpeg(int(camera_id), prefer_overlay=False)
        if not jpeg:
            return None, None
        where = f"{store_name}, {camera_name}" if store_name else camera_name
        base = str(getattr(settings, "verifier_ollama_url",
                           "http://host.docker.internal:11434")).rstrip("/")
        data = _post_json(
            base + "/api/chat", headers={},
            payload={
                "model": settings.scene_review_model,
                "stream": False,
                "format": "json",
                "options": {"num_predict": 120, "temperature": 0.1},
                "messages": [
                    {"role": "system", "content": _VLM_SYSTEM},
                    {"role": "user",
                     "content": (
                         f"Camera: {where}. The microphone just picked up a "
                         f"sound about {ratio:.0f} times louder than this "
                         f"room's normal level, with the harmonic character "
                         f"of a raised voice. Describe what is visible in "
                         f"this frame. Reply as JSON: "
                         f'{{"description": "<one sentence>"}}'),
                     "images": [base64.b64encode(jpeg).decode()]},
                ],
            },
            timeout=float(getattr(settings, "scene_review_timeout_seconds", 90)))
        raw = str((data.get("message") or {}).get("content") or "").strip()
        if not raw:
            return None, jpeg
        import json as _json
        try:
            desc = str(_json.loads(raw).get("description") or "").strip()
        except (ValueError, AttributeError):
            desc = raw
        desc = " ".join(desc.split())
        return (desc or None), jpeg
    except Exception as exc:                                # noqa: BLE001
        log.info("audio: scene description unavailable (%s)", exc)
        return None, None


@celery_app.task(name="audio.monitor_sweep")
def audio_monitor_sweep() -> dict:
    """Sample every audio-capable camera once and alert on raised voices.

    Sampling is serial and each camera costs `audio_sample_seconds` plus
    connection setup - measured at 179s for 21 cameras. A lock is
    therefore mandatory, not defensive: without it a slow sweep is still
    running when the next one starts and they stack until the alerts
    worker has no free slot. Serial is deliberate too; this host already
    leaks ffmpeg processes, so firing several at once would make a known
    problem worse.
    """
    if not bool(getattr(settings, "audio_monitor_enabled", False)):
        return {"skipped": "disabled"}

    from app.ai.detectors import sustained_state
    from app.database import SessionLocal
    from app.models import Camera
    from app.tasks.alerting import _create_info_alert, _redis
    from app.utils.crypto import decrypt
    from app.utils.network import build_rtsp_url

    seconds = int(getattr(settings, "audio_sample_seconds", 6))
    cooldown = float(getattr(settings, "audio_cooldown_seconds", 600))
    db = SessionLocal()
    r = _redis()
    checked = fired = 0

    # One sweep at a time. The TTL is a dead-man's switch: if a worker is
    # killed mid-sweep the lock expires on its own rather than silencing
    # audio monitoring until someone notices.
    lock_key = "vg:audio:sweep_lock"
    try:
        if not r.set(lock_key, "1", nx=True, ex=900):
            log.info("audio sweep: previous sweep still running, skipping")
            db.close()
            return {"skipped": "locked"}
    except Exception as exc:                                # noqa: BLE001
        log.warning("audio sweep: lock unavailable (%s) - proceeding", exc)

    try:
        for cid in capable_camera_ids(db, r):
            cam = db.query(Camera).filter(Camera.id == cid).first()
            if not cam or cam.is_deleted or not cam.ai_enabled:
                continue
            try:
                pw = decrypt(cam.password_encrypted)
            except Exception:                               # noqa: BLE001
                continue
            url = build_rtsp_url(brand=cam.brand or "hikvision", host=cam.host,
                                 port=cam.rtsp_port or 554,
                                 username=cam.username, password=pw,
                                 channel=cam.channel_number or 1, subtype=0)
            # The buffer is held past analyse() only so a sample that
            # fires can be attached to its alert. Every path that does
            # not alert drops it explicitly below rather than relying on
            # the next iteration to rebind the name.
            pcm = _pcm(url, seconds)
            feat = analyse(pcm)
            if not feat:
                pcm = b""
                continue
            checked += 1

            base = _baseline(r, cid, feat["rms"], update=True)
            raised, ratio = is_raised_voice(feat, base)
            if not raised:
                # Near-miss logging. The flatness ceiling was calibrated
                # against ambient only (measured 0.67-0.90 on live
                # cameras), so the cut-off is still an estimate. Logging
                # loud-but-rejected samples shows where real events fall
                # and turns the threshold into a measurement instead of
                # a second guess. Near misses are logged as NUMBERS, not
                # kept as audio: only a sample that alerts is written.
                # Confirmed alerts now carry their sound, so the ceiling
                # can finally be checked against shouts operators have
                # actually marked true.
                if ratio >= float(getattr(settings, "audio_rms_ratio", 3.0)):
                    log.info("audio near-miss camera=%s ratio=%.1f "
                             "speech=%.2f flatness=%.3f (ceiling %.2f)",
                             cid, ratio, feat["speech_share"],
                             feat.get("flatness", 1.0),
                             float(getattr(settings, "audio_flatness_max", 0.45)))
                pcm = b""
                continue
            # Recompute the baseline WITHOUT this sample so a sustained
            # incident cannot normalise itself away.
            _baseline(r, cid, feat["rms"], update=False)

            if not sustained_state.fire_allowed(
                    cid, None, "audio_distress", cooldown_seconds=cooldown):
                pcm = b""
                continue
            store_name = None
            try:
                from app.models import Store
                st = db.query(Store).filter(Store.id == cam.store_id).first()
                store_name = st.name if st else None
            except Exception:                               # noqa: BLE001
                pass
            vlm_desc, jpeg = describe_scene(cid, cam.name, store_name, ratio)
            thumb = None
            if jpeg:
                try:
                    from app.tasks.scene_review import _save_frame
                    thumb = _save_frame(int(cid), jpeg)
                except Exception:                           # noqa: BLE001
                    thumb = None

            # The sample is encoded only now, on the far side of every
            # threshold and cooldown check, so nothing is written for a
            # camera that did not alert. Dropped immediately after.
            audio_clip = _save_audio_sample(int(cid), pcm)
            pcm = b""

            headline = (vlm_desc or
                        f"Raised voices heard at {cam.name}.")
            _create_info_alert(
                db, camera_id=int(cid), zone_id=None, store_id=cam.store_id,
                detection_type="audio_distress", cls="raised_voice",
                thumbnail_path=thumb,
                extra={
                    "priority": "high",
                    "description": headline,
                    "vlm_description": vlm_desc,
                    # Server-internal path, like thumbnail_path and
                    # alert_clip_path. The API exposes a URL, never this.
                    "audio_clip_path": audio_clip,
                    "audio_clip_seconds": (round(feat["seconds"], 1)
                                           if audio_clip else None),
                    "audio_note": (
                        ("Detected on the microphone. The sample that "
                         "triggered this alert is kept for 48 hours so it "
                         "can be heard; nothing else is recorded and "
                         "nothing is transcribed.")
                        if audio_clip else
                        "Detected on the microphone. No recording is kept "
                        "and nothing is transcribed — only loudness and "
                        "frequency were measured."),
                    "loudness_ratio": round(ratio, 2),
                    "speech_share": round(feat["speech_share"], 3),
                    "flatness": round(feat.get("flatness", 1.0), 3),
                    "baseline_rms": round(base, 5),
                    "sample_seconds": round(feat["seconds"], 1),
                    "camera_name": cam.name,
                    "store_id": cam.store_id,
                },
            )
            fired += 1
        db.commit()
    finally:
        db.close()
        try:
            r.delete(lock_key)
        except Exception:                                   # noqa: BLE001
            pass
    log.info("audio sweep: %d cameras sampled, %d alerts", checked, fired)
    return {"checked": checked, "alerts": fired}


@celery_app.task(name="audio.prune_clips", ignore_result=True)
def prune_audio_clips() -> dict:
    """Delete audio samples past their retention window and clear the path.

    Deliberately simpler than recorder.prune_alert_clips, which exempts
    open and escalated alerts from pruning so video evidence survives an
    investigation. Voice gets no such exemption: an alert nobody got
    round to resolving is not a lawful basis for keeping a recording of
    staff speaking, so this prunes purely on age.

    Also sweeps orphaned files — a sample whose encode succeeded but
    whose alert insert then failed would otherwise sit on disk forever
    with no row pointing at it.
    """
    from app.database import SessionLocal
    from app.models import DetectionEvent

    hours = int(getattr(settings, "audio_clip_retention_hours", 48))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cleared = 0
    with SessionLocal() as db:
        rows = (db.query(DetectionEvent)
                  .filter(DetectionEvent.detection_type == "audio_distress",
                          DetectionEvent.timestamp < cutoff)
                  .all())
        for ev in rows:
            p = (ev.extra or {}).get("audio_clip_path")
            if not p:
                continue
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:                               # noqa: BLE001
                pass
            ev.extra = {k: v for k, v in (ev.extra or {}).items()
                        if k not in ("audio_clip_path", "audio_clip_seconds")}
            cleared += 1
        if cleared:
            db.commit()

    orphans = _prune_orphaned_audio_dirs(cutoff)
    if cleared or orphans:
        log.info("audio: pruned %d samples, %d orphaned files",
                 cleared, orphans)
    return {"pruned": cleared, "orphans": orphans}


def _prune_orphaned_audio_dirs(cutoff: datetime) -> int:
    """Remove audio files older than the cutoff by mtime, then drop any
    day directory left empty. Dated directories make this cheap: only
    folders older than the window are walked at all."""
    removed = 0
    try:
        root = Path(settings.recordings_dir) / "audio_clips"
        if not root.is_dir():
            return 0
        for day_dir in root.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                day = datetime.strptime(day_dir.name, "%Y-%m-%d")
            except ValueError:
                continue
            # Strictly before the cutoff DAY: everything inside is then
            # unambiguously past retention, and today's folder — which a
            # sweep may be writing into right now — is never touched.
            if day.date() >= cutoff.date():
                continue
            for f in day_dir.glob("*.m4a"):
                try:
                    f.unlink(missing_ok=True)
                    removed += 1
                except Exception:                           # noqa: BLE001
                    pass
            try:
                day_dir.rmdir()
            except OSError:
                pass
    except Exception as e:                                  # noqa: BLE001
        log.warning("audio: orphan prune failed (%s)", e)
    return removed
