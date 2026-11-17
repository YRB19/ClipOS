import requests

from app.config import settings

NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1"


def nvidia_chat_completion(model: str, messages: list[dict], **kwargs) -> dict:
    """Shared NVIDIA NIM chat completion client.

    Used for LLM calls (highlight detection, titling).
    NOT used for Riva ASR — that uses gRPC, see transcribe.py.
    """
    api_key = settings.nvidia_api_key
    if not api_key:
        raise ValueError("NVIDIA_API_KEY is not set — add it to your .env file")

    headers = {"Authorization": f"Bearer {api_key}"}


    request_timeout = kwargs.pop("request_timeout", settings.llm_request_timeout_seconds)
    resp = requests.post(
        f"{NVIDIA_API_BASE}/chat/completions",
        headers=headers,
        json={"model": model, "messages": messages, **kwargs},
        timeout=request_timeout,
    )
    if not resp.ok:
        raise requests.HTTPError(
            f"NVIDIA API error {resp.status_code}",
            response=resp,
        )
    return resp.json()
