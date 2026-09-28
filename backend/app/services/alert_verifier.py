"""AI verification for alerts: annotate, never hide.

verify(alert_id) looks at the alert's frames with a vision model and
writes a verdict NEXT TO the alert (the seven ai_* columns). It must
never suppress, hide, reclassify, downgrade or delay an alert: the
ONLY columns it touches are ai_verdict, ai_confidence, ai_outcome,
ai_action, ai_reason, ai_verified_at and ai_model. It never writes
review_only, notification_suppressed, status, severity or anything
else, and it never blocks or removes anything from the feed.

verify() never raises: any failure records verdict=uncertain with the
error in ai_reason so the operator can see WHY verification failed.

Providers: settings.verifier_provider picks anthropic (SDK), openai or
ollama (both plain httpx). ai_model records "<provider>:<model>".
"""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

VERDICTS = ("true_alert", "false_alert", "uncertain")

# Alert types with no meaningful frame to judge (heartbeats, BI
# updates, counters, offline markers). These short-circuit to
# uncertain WITHOUT an API call. Override the set via
# settings.verifier_non_visual_types (env VERIFIER_NON_VISUAL_TYPES,
# JSON list).
NON_VISUAL_TYPES = frozenset({
    "camera_offline", "system_health", "live_activity",
    "store_intelligence", "sales_floor_insight", "entry_exit",
    "occupancy", "occupancy_metrics", "unique_visitor",
    "heatmap", "customer_journey", "demographic",
})


def _non_visual_types() -> frozenset:
    override = getattr(settings, "verifier_non_visual_types", None)
    try:
        if override:
            return frozenset(str(t) for t in override)
    except Exception:
        pass
    return NON_VISUAL_TYPES

# One paragraph per alertable detection_type: what the real Vivo case
# looks like and what commonly false-triggers it. Anything not listed
# falls back to DEFAULT_SCENARIO.
SCENARIOS: dict[str, str] = {
    "intrusion": (
        "A person inside the store outside trading hours. Real cases are "
        "break-ins or someone hiding at closing. Common false triggers: "
        "mannequins near the entrance, staff opening or closing the store "
        "(uniform, keys, routine movements, lights being switched), "
        "cleaners with mops or buckets, security guards on patrol, and "
        "reflections or headlights sweeping the glass shopfront."
    ),
    "person": (
        "A person detected where or when people are notable, usually "
        "before or after trading hours. Real cases match intrusion. "
        "Common false triggers: mannequins, posters with life-size "
        "models, staff arriving early to open, cleaners, and reflections "
        "in mirrors or the shopfront glass."
    ),
    "trespass": (
        "A customer inside a staff-only or restricted zone such as a "
        "stockroom corridor or behind a service counter. Real cases show "
        "a non-uniformed person clearly past the boundary. Common false "
        "triggers: staff without visible uniform, a customer leaning "
        "over the counter to pay or point, children wandering a step "
        "past the line, and zone polygons that clip a walkway."
    ),
    "staff_present": (
        "The service counter left unattended. Real cases show an empty "
        "counter zone with customers waiting. Common false triggers: "
        "staff kneeling below camera view to arrange stock, staff "
        "standing at the counter edge just outside the zone, and "
        "mannequins or poster figures counted as customers."
    ),
    "staff_zone": (
        "An unidentified or non-uniformed person behind the counter. "
        "Real cases show someone with no staff uniform handling the "
        "till. Common false triggers: staff who removed a jacket or "
        "lanyard, new staff whose uniform is not yet recognised, and "
        "customers leaning across the counter."
    ),
    "uniform_compliance": (
        "A person at the service counter without the Vivo staff uniform "
        "or name tag. Real cases show an unidentified person serving. "
        "Common false triggers: staff in transit wearing a coat, "
        "delivery riders handing over parcels, and lighting that washes "
        "out the uniform colour."
    ),
    "queue": (
        "A long checkout queue. Real cases show several distinct "
        "customers waiting in line. Common false triggers: mannequins "
        "in or near the queue zone, a family group standing together, "
        "staff restocking inside the zone, and browsers who are not "
        "actually queueing."
    ),
    "crowd": (
        "Too many people concentrated in one area. Real cases show a "
        "dense group blocking an aisle. Common false triggers: "
        "mannequin clusters, promotional displays with figures, and a "
        "short-lived family group passing through."
    ),
    "occupancy": (
        "Store occupancy above the configured capacity. Real cases show "
        "a genuinely full sales floor. Common false triggers: double "
        "counting from overlapping cameras and mannequins inflating the "
        "person count."
    ),
    "occupancy_metrics": (
        "Occupancy metrics above threshold. Same real and false cases "
        "as occupancy: overlapping camera double counts and mannequins."
    ),
    "checkout_dwell": (
        "A customer at the checkout for an unusually long time. Real "
        "cases involve payment problems, disputes or suspicious "
        "behaviour at the till. Common false triggers: staff doing "
        "till administration, a customer chatting after paying, and "
        "track identity switches merging two visits into one."
    ),
    "loitering": (
        "A person lingering in one area far longer than browsing "
        "normally takes. Real cases can precede shoplifting. Common "
        "false triggers: mannequins (a static figure is the classic "
        "one), staff stationed at a fitting room, waiting companions "
        "sitting near fitting rooms, and children playing."
    ),
    "abandoned_object": (
        "A bag or item left unattended on the floor. Real cases show "
        "the same object static with no owner nearby. Common false "
        "triggers: shopping baskets set down while browsing, stock "
        "cartons during restocking, cleaning equipment, and bags at a "
        "waiting companion's feet."
    ),
    "fall": (
        "A person who may have fallen. Real cases show someone on the "
        "floor not getting up. Common false triggers: staff kneeling to "
        "arrange low shelves, customers crouching to try shoes, "
        "children sitting on the floor, and mannequins being laid down "
        "during display changes."
    ),
    "fight": (
        "A physical altercation. Real cases show rapid aggressive "
        "contact between people. Common false triggers: friends "
        "greeting or hugging, animated bargaining gestures, staff "
        "moving mannequins, and children playing."
    ),
    "weapon": (
        "A weapon visible in the store. Real cases are robbery or an "
        "armed intruder. Common false triggers: umbrellas, clothes "
        "hangers, steamer hoses, belts held up to the light, and "
        "phones held in unusual grips."
    ),
    "weapon_brandished": (
        "A weapon actively brandished at people. Real cases show "
        "threatening posture and fleeing bystanders. Common false "
        "triggers are the same objects as weapon plus staff gesturing "
        "with tools during display work."
    ),
    "fire": (
        "Possible fire inside or near the store. Real cases show flame "
        "or growing smoke. Common false triggers: warm-toned lighting, "
        "reflections of sunlight, red or orange garments and displays, "
        "and dust on the lens catching light."
    ),
    "smoke": (
        "Possible smoke. Real cases show a spreading haze. Common "
        "false triggers: steamers used on garments, dust clouds during "
        "cleaning, fog outside the shopfront, and lens glare."
    ),
    "shrinkage": (
        "Suspicious activity near products, possibly concealment. Real "
        "cases show items moved into bags or clothing. Common false "
        "triggers: customers holding items while browsing, trying "
        "garments over their clothes, staff restocking quickly, and "
        "customers kneeling at low shelves."
    ),
    "shutter": (
        "Store shutter or door in the wrong state for the hour. Real "
        "cases: shutter open long after closing or still closed after "
        "opening time. Common false triggers: partial shutter positions "
        "during cleaning, deliveries outside hours, and lighting "
        "changes confusing the open/closed classifier."
    ),
    "shop_open_close": (
        "Store opening or closing state change, or a store that did "
        "not open by the expected time. Real cases: no staff arrival by "
        "the cutoff. Common false triggers: entrance cameras offline "
        "(absence of data is not absence of staff), staff entering "
        "through a back door, and openings earlier than the camera "
        "recording window."
    ),
    "tailgating": (
        "Two people entering on one authorisation where single entry is "
        "expected. Common false triggers: families entering together, "
        "staff holding the door for customers, and trolleys or prams "
        "read as a second person."
    ),
    "entry_exit": (
        "A person crossing the entrance line. Real cases are ordinary "
        "footfall; this alert type is usually informational. Common "
        "false triggers: mannequins near the door, staff standing in "
        "the doorway, and reflections in the glass."
    ),
    "dwell": (
        "Unusually long dwell in a monitored zone. Same real and false "
        "cases as loitering: mannequins, stationed staff and waiting "
        "companions are the classic false triggers."
    ),
    "stockroom_access": (
        "A person entering the stockroom. Real cases show non-staff "
        "entering. Common false triggers: staff without visible "
        "uniform, cleaners, and delivery crews during receiving hours."
    ),
    "live_activity": (
        "Sustained people activity flagged by the occupancy sentinel. "
        "Real cases show several genuine customers. The classic false "
        "trigger is mannequins counted as people, plus posters and "
        "reflections."
    ),
    "store_intelligence": (
        "A periodic store business update, not a security event. It is "
        "almost always a true informational update; verify only that "
        "the scene matches an open, operating store."
    ),
    "sales_floor_insight": (
        "A sales-floor status heartbeat, not a security event. Treat "
        "as informational; verify the scene is consistent with the "
        "reported state."
    ),
    "camera_offline": (
        "A camera stopped streaming. There are usually no frames to "
        "judge; base the verdict on whatever frame is available and "
        "say so in the reason."
    ),
}

DEFAULT_SCENARIO = (
    "A retail store camera event. Real cases involve people doing "
    "something the detector describes. Common false triggers across "
    "all Vivo detectors: mannequins, reflections in mirrors and the "
    "shopfront glass, cleaners, staff opening or closing the store, "
    "staff kneeling to arrange stock, children, bags left on the "
    "floor, customers leaning over counters, and lighting changes."
)

_SYSTEM_TEMPLATE = (
    "You are a retail loss-prevention reviewer for Vivo Fashion Group "
    "stores in Kenya. An automated detector raised {detection_type}. "
    "Scenario context: {scenario} Camera: {camera}. Store: {store}. "
    "Local time: {when}. Store is {open_state}. Look at the frames and "
    "answer ONLY with JSON: {{\"verdict\": \"true_alert\" or "
    "\"false_alert\" or \"uncertain\", \"confidence\": <number 0-1>, "
    "\"likely_outcome\": \"<one sentence: what happens if nobody "
    "acts>\", \"recommended_action\": \"<one imperative sentence for "
    "the store manager>\", \"reason\": \"<one sentence on what in the "
    "frames decided it>\"}}"
)


def _normalise_verdict(raw: str) -> str:
    v = (raw or "").strip().lower()
    if v in ("true_alert", "true", "real", "genuine"):
        return "true_alert"
    if v in ("false_alert", "false", "likely_false", "not_real"):
        return "false_alert"
    return "uncertain"


def _extract_json(text: str) -> dict:
    """Parse the model reply. Tolerates prose or code fences around the
    JSON object; raises ValueError when no object can be parsed."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model reply")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("model reply is not a JSON object")
    return data


def _collect_images(alert, event) -> list[str]:
    """Up to settings.verifier_max_images base64 JPEGs, chronological:
    nearest pre-frame from the filmstrip, the alert snapshot, nearest
    post-frame. Missing files are skipped; may return []."""
    max_images = max(1, int(getattr(settings, "verifier_max_images", 3)))
    thumb = getattr(event, "thumbnail_path", None) if event is not None else None
    strip = [p for p in (getattr(alert, "snapshot_paths", None) or [])
             if p and p != thumb and Path(p).exists()]
    ordered: list[str] = []
    if strip:
        ordered.append(strip[0])                 # nearest pre-frame
    if thumb and Path(thumb).exists():
        ordered.append(thumb)                    # the alert snapshot
    if len(strip) > 1:
        ordered.append(strip[-1])                # nearest post-frame
    if not ordered and strip:
        ordered = strip[:max_images]
    out: list[str] = []
    for p in ordered[:max_images]:
        try:
            out.append(base64.standard_b64encode(
                Path(p).read_bytes()).decode("ascii"))
        except Exception as e:
            log.debug("verifier: could not read frame %s: %s", p, e)
    return out


def _is_network_error(exc: Exception) -> bool:
    try:
        import anthropic
        net = tuple(t for t in (
            getattr(anthropic, "APIConnectionError", None),
            getattr(anthropic, "APITimeoutError", None),
        ) if t is not None)
        if net and isinstance(exc, net):
            return True
    except Exception:
        pass
    try:
        import httpx
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout,
                            httpx.ReadTimeout, httpx.RemoteProtocolError)):
            return True
    except Exception:
        pass
    return isinstance(exc, (ConnectionError, TimeoutError))


_MAX_TOKENS = 300


def _provider() -> str:
    p = str(getattr(settings, "verifier_provider", "anthropic")
            or "anthropic").strip().lower()
    return p if p in ("anthropic", "openai", "ollama") else "anthropic"


def _provider_model(provider: str) -> str:
    if provider == "openai":
        return str(getattr(settings, "verifier_openai_model", "gpt-4o-mini"))
    if provider == "ollama":
        return str(getattr(settings, "verifier_ollama_model",
                           "qwen2.5vl:7b"))
    return str(getattr(settings, "verifier_model", "claude-sonnet-4-6"))


def _user_text(images_b64: list[str]) -> str:
    return (("Frames are in chronological order."
             if images_b64 else
             "No frames are available for this alert; judge from the "
             "context alone and lower your confidence accordingly.")
            + " Reply with the JSON only.")


def _post_json(url: str, *, headers: dict, payload: dict,
               timeout: float = 30.0) -> dict:
    """Single HTTP seam for the openai/ollama providers - tests fake
    the transport by monkeypatching this function."""
    import httpx
    with httpx.Client(timeout=timeout) as c:
        r = c.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


def _call_anthropic(system_prompt: str, images_b64: list[str],
                    max_tokens: int) -> str:
    import anthropic
    api_key = str(getattr(settings, "anthropic_api_key", "") or "")
    if not api_key:
        raise RuntimeError("anthropic_api_key not configured")
    client = anthropic.Anthropic(api_key=api_key, timeout=30.0)
    content: list[dict] = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg",
                    "data": b64}}
        for b64 in images_b64
    ]
    content.append({"type": "text", "text": _user_text(images_b64)})
    msg = client.messages.create(
        model=_provider_model("anthropic"),
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(getattr(b, "text", "") or "" for b in (msg.content or []))


def _call_openai(system_prompt: str, images_b64: list[str],
                 max_tokens: int) -> str:
    api_key = str(getattr(settings, "openai_api_key", "") or "")
    if not api_key:
        raise RuntimeError("openai_api_key not configured")
    content: list[dict] = [
        {"type": "image_url",
         "image_url": {"url": "data:image/jpeg;base64," + b64}}
        for b64 in images_b64
    ]
    content.append({"type": "text", "text": _user_text(images_b64)})
    data = _post_json(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer " + api_key},
        payload={
            "model": _provider_model("openai"),
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
        })
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError("unexpected openai response shape: %r" % (e,))


def _call_ollama(system_prompt: str, images_b64: list[str],
                 max_tokens: int) -> str:
    base = str(getattr(settings, "verifier_ollama_url",
                       "http://host.docker.internal:11434")).rstrip("/")
    data = _post_json(
        base + "/api/chat",
        headers={},
        payload={
            "model": _provider_model("ollama"),
            "stream": False,
            "options": {"num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _user_text(images_b64),
                 "images": images_b64},
            ],
        },
        timeout=60.0)   # local models are slower than cloud APIs
    return str((data.get("message") or {}).get("content") or "")


_CALLERS = {"anthropic": _call_anthropic, "openai": _call_openai,
            "ollama": _call_ollama}


def verify(alert_id: int) -> None:
    """Verify one alert and write the seven ai_* columns. Never raises;
    never touches any other column. One retry after 5s on network
    errors, then the failure is recorded as verdict=uncertain."""
    from app.database import SessionLocal
    from app.models import Alert, Camera, DetectionEvent, Store

    provider = _provider()
    # ai_model records "<provider>:<model>" so operators can see which
    # engine produced each verdict.
    model_name = provider + ":" + _provider_model(provider)
    try:
        with SessionLocal() as db:
            alert = db.get(Alert, alert_id)
            if alert is None:
                return
            event = db.get(DetectionEvent, alert.event_id)
            camera = (db.get(Camera, event.camera_id)
                      if event is not None and event.camera_id else None)
            store = (db.get(Store, camera.store_id)
                     if camera is not None and camera.store_id else None)
            detection_type = ((event.detection_type if event else None)
                              or "unknown")
            if detection_type in _non_visual_types():
                # Nothing visual to judge - record that honestly and
                # skip the API call entirely.
                alert.ai_verdict = "uncertain"
                alert.ai_confidence = 0.0
                alert.ai_reason = "non-visual alert type"
                alert.ai_verified_at = datetime.now(timezone.utc)
                alert.ai_model = "none"
                db.commit()
                return
            scenario = SCENARIOS.get(detection_type, DEFAULT_SCENARIO)

            open_state = "CLOSED"
            when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            try:
                from app.utils.business_hours import (
                    _store_local_now, is_store_open,
                )
                if store is not None:
                    open_state = "OPEN" if is_store_open(store) else "CLOSED"
                    when = _store_local_now(store).strftime(
                        "%A %d %B %Y, %H:%M")
            except Exception:
                pass

            system = _SYSTEM_TEMPLATE.format(
                detection_type=detection_type,
                scenario=scenario,
                camera=(camera.name if camera else "unknown camera"),
                store=(store.name if store else "unknown store"),
                when=when,
                open_state=open_state,
            )
            images = _collect_images(alert, event)

            caller = _CALLERS[provider]
            try:
                text = caller(system, images, _MAX_TOKENS)
            except Exception as first_err:
                if not _is_network_error(first_err):
                    raise
                log.warning("verifier: network error for alert %s, "
                            "retrying in 5s: %s", alert_id, first_err)
                time.sleep(5)
                text = caller(system, images, _MAX_TOKENS)

            data = _extract_json(text)
            try:
                confidence = float(data.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            alert.ai_verdict = _normalise_verdict(str(data.get("verdict", "")))
            alert.ai_confidence = min(1.0, max(0.0, confidence))
            alert.ai_outcome = (str(data.get("likely_outcome") or "")[:500]
                                or None)
            alert.ai_action = (str(data.get("recommended_action") or "")[:500]
                               or None)
            alert.ai_reason = (str(data.get("reason") or "")[:500] or None)
            alert.ai_verified_at = datetime.now(timezone.utc)
            alert.ai_model = model_name
            db.commit()
            log.info("verifier: alert=%s verdict=%s confidence=%.2f",
                     alert_id, alert.ai_verdict, alert.ai_confidence)
    except Exception as e:
        log.warning("verifier: alert=%s failed: %s", alert_id, e)
        try:
            with SessionLocal() as db:
                alert = db.get(Alert, alert_id)
                if alert is None:
                    return
                alert.ai_verdict = "uncertain"
                alert.ai_confidence = 0.0
                alert.ai_reason = str(e)[:120]
                alert.ai_verified_at = datetime.now(timezone.utc)
                alert.ai_model = model_name
                db.commit()
        except Exception:
            log.exception("verifier: could not record failure for alert %s",
                          alert_id)
