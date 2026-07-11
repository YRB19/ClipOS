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

    print(f"[nvidia_client] Headers: {{'Authorization': 'Bearer {api_key[:4]}...{api_key[-4:]}'}}")

    resp = requests.post(
        f"{NVIDIA_API_BASE}/chat/completions",
        headers=headers,
        json={"model": model, "messages": messages, **kwargs},
        timeout=300,  # GLM-5.2 can be slow on free tier with long transcripts — tune based on observed latency
    )
    if not resp.ok:
        raise requests.HTTPError(
            f"NVIDIA API error {resp.status_code}: {resp.text}",
            response=resp,
        )
    return resp.json()
