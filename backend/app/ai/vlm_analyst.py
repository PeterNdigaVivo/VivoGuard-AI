"""Cloud VLM scene understanding (Sprint 2.1).

Adds a concise natural-language scene description to alert-worthy
events via Anthropic Claude (Haiku by default). Modelled on
DeepCamera's skills/analysis VLM integration, but cloud-only — the
Hetzner host is CPU-only, so no local VLM.

INVOKED ASYNC from the vlm.analyse_alert_scene Celery task on the
alerts queue — never on the inference hot path. analyse() is
strictly best-effort: missing key, missing SDK, unreadable image,
timeout, or API error all return None and the alert proceeds
normally.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a retail security analyst reviewing CCTV footage from a "
    "Vivo fashion store in Kenya. Be concise and factual."
)

# detection_type → operator-facing question. {duration} is filled from
# context when present. Anything not listed falls back to GENERIC.
_PROMPTS: dict[str, str] = {
    "checkout_dwell": (
        "Someone has been at the checkout counter for {duration}. "
        "Describe what you see. Is this a customer being served, "
        "staff working, or something else?"
    ),
    "staff_present": (
        "This is the checkout counter area. Is there a staff member "
        "present or is the counter unattended?"
    ),
    "trespass": (
        "Describe the person and activity in this scene. Is this "
        "normal retail activity or something concerning?"
    ),
    "intrusion": (
        "Describe the person and activity in this scene. Is this "
        "normal retail activity or something concerning?"
    ),
    "shop_open_close": (
        "This is the store entrance at opening time. Does the store "
        "appear open or closed?"
    ),
    "uniform_compliance": (
        "Is the person wearing an all-black uniform (black top and "
        "black bottom)?"
    ),
}
_GENERIC = ("Describe what you see in this retail CCTV frame, factually "
            "and concisely.")

_MAX_TOKENS = 200


class VLMAnalyst:
    """Thin wrapper over the Anthropic vision API. One instance per
    Celery task invocation is fine — construction does no I/O."""

    def __init__(self) -> None:
        from app.config import settings
        self.enabled = bool(getattr(settings, "vlm_enabled", False))
        self.api_key = getattr(settings, "anthropic_api_key", "") or ""
        self.model = getattr(settings, "vlm_model", "claude-haiku-4-5")
        self.timeout = int(getattr(settings, "vlm_timeout_seconds", 10))

    def _prompt_for(self, detection_type: str, context: Optional[dict]) -> str:
        tmpl = _PROMPTS.get(detection_type or "", _GENERIC)
        ctx = context or {}
        duration = ctx.get("duration") or ctx.get("duration_str") or "a while"
        try:
            return tmpl.format(duration=duration)
        except Exception:
            return tmpl

    def analyse(self, image_path: str, detection_type: str,
                context: Optional[dict] = None) -> Optional[str]:
        """Return a plain-English scene description, or None on ANY
        failure (disabled, no key, no SDK, unreadable image, timeout,
        API error). Never raises."""
        if not self.enabled or not self.api_key:
            return None
        if not image_path:
            return None
        try:
            p = Path(image_path)
            if not p.exists():
                return None
            raw = p.read_bytes()
            if not raw:
                return None
            media_type = (mimetypes.guess_type(str(p))[0] or "image/jpeg")
            b64 = base64.standard_b64encode(raw).decode("ascii")
        except Exception as e:
            log.debug("vlm: image read failed %s: %s", image_path, e)
            return None

        try:
            import anthropic
        except Exception:
            log.warning("vlm: anthropic SDK not installed — skipping")
            return None

        try:
            client = anthropic.Anthropic(api_key=self.api_key,
                                         timeout=float(self.timeout))
            msg = client.messages.create(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image",
                         "source": {"type": "base64",
                                    "media_type": media_type,
                                    "data": b64}},
                        {"type": "text",
                         "text": self._prompt_for(detection_type, context)},
                    ],
                }],
            )
            # Concatenate any text blocks in the response.
            parts = []
            for block in (msg.content or []):
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            out = " ".join(parts).strip()
            return out or None
        except Exception as e:
            log.warning("vlm: analyse failed (model=%s): %s", self.model, e)
            return None
