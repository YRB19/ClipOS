"""Redis RQ queue setup for ClipOS pipeline orchestration."""

import os
# macOS fork safety: must be set BEFORE importing cv2/av/faster_whisper
os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

import logging

from redis import Redis
from rq import Queue

from app.config import settings

logger = logging.getLogger(__name__)


def _get_redis() -> Redis:
    return Redis.from_url(settings.redis_url)


def get_queue() -> Queue:
    return Queue(connection=_get_redis())


def enqueue_pipeline(job_id: str) -> str:
    """Enqueue run_pipeline(job_id) onto the default RQ queue.

    Returns the RQ job id (not the ClipOS job_id).
    """
    from app.core.pipeline import run_pipeline  # avoid circular import

    q = get_queue()

    # job_timeout=30m: The full pipeline runs faster-whisper transcription
    # (CPU/int8, can take 5-10min on longer videos), multiple ffmpeg calls
    # for clip cutting + reframing, multiple LLM calls (highlights + scoring),
    # and per-clip caption generation. RQ's default of 180s kills the worker
    # mid-transcription on anything beyond short clips. 30 minutes gives
    # headroom for ~20min videos on slower hardware without false timeouts.
    # Do NOT reduce this without profiling the full pipeline on a 15+ min video.
    #
    # result_ttl=86400 (24h): Keeps finished job metadata in Redis long enough
    # for the frontend queue_status polling to read it. Default is 500s which
    # can silently expire before R checks the result.
    rq_job = q.enqueue(
        run_pipeline,
        job_id,
        job_timeout="30m",
        result_ttl=86400,
    )
    logger.info("Enqueued pipeline for job %s (rq_job=%s)", job_id, rq_job.id)
    return rq_job.id
