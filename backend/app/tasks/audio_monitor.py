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

  * never writes audio to disk — PCM is piped from ffmpeg, reduced to a
    handful of floats, and the buffer is dropped;
  * never transcribes, and holds no speech-recognition code path;
  * stores only derived numbers (loudness ratio, band shares) on the
    event.

It answers "someone is shouting", never "someone said X". That boundary
is what makes it defensible, so keep it.

Only ~22 of 97 reachable cameras have a microphone at all, and every one
observed is NVR channel 1, so coverage is roughly one camera per store.
"""
from __future__ import annotations

import logging
import subprocess
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
            feat = analyse(_pcm(url, seconds))
            if not feat:
                continue
            checked += 1

            base = _baseline(r, cid, feat["rms"], update=True)
            raised, ratio = is_raised_voice(feat, base)
            if not raised:
                # Near-miss logging. The flatness ceiling is calibrated
                # against ambient only (measured 0.67-0.90 on live
                # cameras); no recording of an actual shout exists yet,
                # so the cut-off is an estimate. Logging loud-but-
                # rejected samples shows where real events actually fall
                # and turns the threshold into a measurement instead of
                # a second guess.
                if ratio >= float(getattr(settings, "audio_rms_ratio", 3.0)):
                    log.info("audio near-miss camera=%s ratio=%.1f "
                             "speech=%.2f flatness=%.3f (ceiling %.2f)",
                             cid, ratio, feat["speech_share"],
                             feat.get("flatness", 1.0),
                             float(getattr(settings, "audio_flatness_max", 0.45)))
                continue
            # Recompute the baseline WITHOUT this sample so a sustained
            # incident cannot normalise itself away.
            _baseline(r, cid, feat["rms"], update=False)

            if not sustained_state.fire_allowed(
                    cid, None, "audio_distress", cooldown_seconds=cooldown):
                continue
            _create_info_alert(
                db, camera_id=int(cid), zone_id=None, store_id=cam.store_id,
                detection_type="audio_distress", cls="raised_voice",
                extra={
                    "priority": "high",
                    "description": (
                        "Raised voices detected on the microphone at "
                        f"{cam.name}. No recording is kept and nothing is "
                        "transcribed — only loudness and frequency were "
                        "measured."),
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
