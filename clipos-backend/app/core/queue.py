"""RQ transport. Durable task IDs are assigned in SQLite before dispatch."""
import os
os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
from datetime import timedelta
from redis import Redis
from rq import Queue
from app.config import settings


def get_queue():
    return Queue(connection=Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3))


def enqueue_stage(task_id, stage, delay=0):
    from app.core.pipeline import run_stage
    queue = get_queue()
    options = dict(job_id=task_id, job_timeout=task_timeout(stage), result_ttl=86400, failure_ttl=604800)
    if delay:
        return queue.enqueue_in(timedelta(seconds=delay), run_stage, task_id, **options).id
    return queue.enqueue(run_stage, task_id, **options).id


def task_timeout(stage):
    return settings.analysis_timeout_seconds if stage in ('assets', 'transcribe', 'highlights', 'score') else settings.media_timeout_seconds + 60


def enqueue_pipeline(job_id):
    from app.core.pipeline import run_pipeline
    return get_queue().enqueue(run_pipeline, job_id, job_timeout=60, result_ttl=86400).id
