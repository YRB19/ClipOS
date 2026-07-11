"""Redis RQ queue setup for ClipOS pipeline orchestration."""

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
    rq_job = q.enqueue(run_pipeline, job_id)
    logger.info("Enqueued pipeline for job %s (rq_job=%s)", job_id, rq_job.id)
    return rq_job.id
