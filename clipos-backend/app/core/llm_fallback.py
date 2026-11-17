"""Configured provider chain: validated responses, bounded retries and safe telemetry."""
import logging
import math
import time
import requests
from sqlmodel import Session
from app.config import settings
from app.models.job import engine
from app.models.selection import ModelCall
import app.core.nvidia_client as _nvidia
import app.core.openrouter_client as _openrouter

logger = logging.getLogger(__name__)


def provider_tiers(vision=False):
    tiers = []
    for spec in settings.vision_models if vision else settings.text_models:
        provider, model = spec.split(':', 1)
        if not model or provider not in ('nvidia', 'openrouter') or (vision and provider != 'openrouter'):
            raise ValueError('Use provider:model entries; vision currently supports OpenRouter')
        module, name = (_nvidia, 'nvidia_chat_completion') if provider == 'nvidia' else (_openrouter, 'openrouter_chat_completion')
        tiers.append((module, name, model))
    return tiers


class ProvidersUnavailable(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def transient(exc):
    return isinstance(exc, (requests.Timeout, requests.ConnectionError, TimeoutError, ConnectionError)) or (
        isinstance(exc, requests.HTTPError) and exc.response is not None and
        (exc.response.status_code == 429 or exc.response.status_code >= 500))


def _number(value, integer=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        return None
    return int(value) if integer else float(value)


def _record(response, *, started, job_id, stage, model, provider, prompt_version, attempt, error, db_engine):
    usage = response.get('usage') or {} if isinstance(response, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    record = ModelCall(job_id=job_id, stage=stage, model=model, provider=provider,
        prompt_version=prompt_version, attempt=attempt, status='failed' if error else 'completed',
        latency_seconds=time.monotonic()-started, error_type=type(error).__name__ if error else None,
        prompt_tokens=_number(usage.get('prompt_tokens'), True),
        completion_tokens=_number(usage.get('completion_tokens'), True), cost_credits=_number(usage.get('cost')) if provider == 'openrouter' else None)
    with Session(db_engine or engine) as session:
        session.add(record)
        session.commit()


def call_llm_with_fallback(messages, provider_tiers=None, temperature=.3, max_tokens=4096,
                           validate=None, job_id=None, stage='text', prompt_version='legacy-v1', db_engine=None):
    """Never treat HTTP 200 as success before semantic validation.

    The complete chain has a monotonic budget; every request uses its remaining
    budget, plus bounded exponential backoff for transient errors. RQ remains the
    hard wall-clock process deadline (requests timeouts are socket deadlines).
    Only error types/status codes are logged, never provider bodies, prompts or keys.
    """
    tiers = provider_tiers if provider_tiers is not None else globals()['provider_tiers']()
    deadline = time.monotonic()+settings.llm_deadline_seconds
    errors, retryable = [], False
    for tier_index, (module, name, model) in enumerate(tiers, 1):
        provider = 'nvidia' if module is _nvidia else 'openrouter'
        for attempt in range(settings.llm_retries+1):
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise ProvidersUnavailable('LLM deadline exhausted', retryable=True)
            started, response, error = time.monotonic(), None, None
            try:
                response = getattr(module, name)(model=model, messages=messages, temperature=temperature,
                    max_tokens=max_tokens, request_timeout=min(settings.llm_request_timeout_seconds, remaining))
                content = response['choices'][0]['message']['content']
                if not isinstance(content, str) or not content.strip():
                    raise ValueError('Empty model content')
                finish = response['choices'][0].get('finish_reason')
                if finish in ('length', 'content_filter', 'error', 'tool_calls') or response['choices'][0].get('error'):
                    raise ValueError('Incomplete model response')
                if validate:
                    validate(content)
                if time.monotonic() > deadline:
                    raise TimeoutError('Provider exceeded request-chain budget')
            except Exception as exc:
                error = exc
            _record(response, started=started, job_id=job_id, stage=stage, model=model, provider=provider,
                    prompt_version=prompt_version, attempt=attempt+1, error=error, db_engine=db_engine)
            if error is None:
                return content, f'tier_{tier_index}:{model}'
            retryable |= transient(error)
            status = error.response.status_code if isinstance(error, requests.HTTPError) and error.response is not None else None
            safe = f'tier_{tier_index} ({model}): {type(error).__name__}' + (f' HTTP {status}' if status else '')
            errors.append(safe)
            logger.warning('%s', safe)
            # Malformed/invalid output falls through to a fresh provider. Only
            # transient transport errors retry the same tier.
            if not transient(error) or attempt == settings.llm_retries:
                break
            delay = min(settings.llm_backoff_seconds*2**attempt, max(0, deadline-time.monotonic()))
            if delay:
                time.sleep(delay)
    raise ProvidersUnavailable('All LLM providers failed: ' + '; '.join(errors), retryable=retryable)
