"""Shared 3-tier LLM fallback chain used by highlights.py and scoring.py."""

import logging

import app.core.nvidia_client as _nvidia
import app.core.openrouter_client as _openrouter

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_TIERS = [
    (_nvidia, "nvidia_chat_completion", "qwen/qwen3.5-397b-a17b"),
    (_nvidia, "nvidia_chat_completion", "mistralai/mistral-nemotron"),
    (_openrouter, "openrouter_chat_completion", "qwen/qwen3-coder:free"),
]


def call_llm_with_fallback(
    messages: list[dict],
    provider_tiers: list[tuple] | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> tuple[str, str]:
    """Call LLM through the provider tier chain until one succeeds.

    Returns:
        (raw_content, provider_used) — the text content from the winning
        provider and a label like "tier_1:qwen/qwen3.5-397b-a17b".

    Raises:
        RuntimeError if all tiers fail. The message lists every tier's error.
    """
    tiers = provider_tiers or DEFAULT_PROVIDER_TIERS
    errors: list[str] = []
    provider_used = None
    raw_content = None

    for tier_idx, (module, fn_name, model) in enumerate(tiers, 1):
        provider_fn = getattr(module, fn_name)
        try:
            response = provider_fn(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            raw_content = response["choices"][0]["message"]["content"]
            provider_used = f"tier_{tier_idx}:{model}"
            break
        except Exception as e:
            errors.append(f"tier_{tier_idx} ({model}): {e}")
            logger.warning("Tier %d failed (%s): %s", tier_idx, model, e)

    if provider_used is None:
        raise RuntimeError(
            "All LLM providers failed:\n" + "\n".join(errors)
        )

    logger.info("LLM served by %s", provider_used)
    return raw_content, provider_used
