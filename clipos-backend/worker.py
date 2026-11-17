#!/usr/bin/env python3
"""RQ worker entrypoint for ClipOS.

Usage:
    python worker.py            # listens on the default queue
    python worker.py high       # listens on a "high" priority queue
    python worker.py low        # listens on a "low" priority queue

Prerequisites:
    - Redis running locally (brew install redis && redis-server)
    - All dependencies installed (pip install -r requirements.txt)
"""

import sys
import logging
import os

from redis import Redis
from rq import Queue, Worker, SpawnWorker

from app.config import settings
from app.models.job import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("worker")


def worker_class(redis_url: str):
    # RQ's SpawnWorker avoids forking a loaded Objective-C runtime on macOS.
    # RQ 2.6.1 cannot reconstruct a Redis Unix-socket connection in its child;
    # keep the existing fork worker for that explicit configuration.
    if sys.platform == 'darwin' and not redis_url.startswith('unix://'):
        return SpawnWorker
    return Worker


def main() -> None:
    queue_names = sys.argv[1:] if len(sys.argv) > 1 else ["default"]
    conn = Redis.from_url(settings.redis_url)

    logger.info("Starting RQ worker on queues: %s", queue_names)
    logger.info("Redis transport: %s", settings.redis_url.split(':', 1)[0])

    # Ensure DB tables exist (worker may start before uvicorn)
    from app.core.media import media_diagnostics
    media_diagnostics()
    init_db()

    queues = [Queue(name, connection=conn) for name in queue_names]
    selected = worker_class(settings.redis_url)
    if sys.platform == 'darwin' and selected is Worker:
        os.environ.setdefault('OBJC_DISABLE_INITIALIZE_FORK_SAFETY', 'YES')
    worker = selected(queues, connection=conn)
    logger.info("Worker class: %s", type(worker).__name__)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
