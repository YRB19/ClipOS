import requests

from app.config import settings

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"


def openrouter_chat_completion(model: str, messages: list[dict], **kwargs) -> dict:
    """OpenRouter chat completion client — used as fallback when NVIDIA NIM is unavailable."""
    api_key = settings.openrouter_api_key
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is not set — add it to your .env file")

    headers = {"Authorization": f"Bearer {api_key}"}

    request_timeout = kwargs.pop("request_timeout", settings.llm_request_timeout_seconds)
    resp = requests.post(
        f"{OPENROUTER_API_BASE}/chat/completions",
        headers=headers,
        json={"model": model, "messages": messages, **kwargs},
        timeout=request_timeout,
    )
    if not resp.ok:
        raise requests.HTTPError(
            f"OpenRouter API error {resp.status_code}",
            response=resp,
        )
    return resp.json()
