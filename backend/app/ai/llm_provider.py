"""Small provider-neutral text-reasoning adapter.

Only monitoring-agent reasoning uses this module.  Detection, alert creation
and simulation policy remain deterministic when every provider is unavailable.
Secrets are supplied by callers from environment-backed settings and are never
logged here.
"""
from __future__ import annotations


class LLMProviderError(RuntimeError):
    """A configured provider could not return a usable text response."""


def complete_text(
    *, provider: str, api_key: str, model: str, system: str, user: str,
    timeout_seconds: float, max_tokens: int,
) -> str:
    """Return provider text or raise ``LLMProviderError``.

    The OpenAI call uses the Responses API with storage disabled.  Both paths
    deliberately expose the same minimal contract so a provider outage cannot
    leak into deterministic agent logic.
    """
    selected = (provider or "").strip().lower()
    if not api_key:
        raise LLMProviderError(f"{selected or 'unknown'} API key is not configured")
    try:
        if selected == "anthropic":
            import anthropic

            client = anthropic.Anthropic(
                api_key=api_key, timeout=float(timeout_seconds))
            response = client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = " ".join(
                getattr(block, "text", "") or ""
                for block in (response.content or []))
        elif selected == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=api_key, timeout=float(timeout_seconds))
            response = client.responses.create(
                model=model, instructions=system, input=user,
                max_output_tokens=max_tokens, store=False,
            )
            text = getattr(response, "output_text", "") or ""
        else:
            raise LLMProviderError(f"unsupported LLM provider: {selected!r}")
    except LLMProviderError:
        raise
    except Exception as exc:
        raise LLMProviderError(
            f"{selected} request failed: {type(exc).__name__}: {exc}") from exc
    text = text.strip()
    if not text:
        raise LLMProviderError(f"{selected} returned an empty response")
    return text
