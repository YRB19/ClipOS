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

from redis import Redis
from rq import Queue, Worker

from app.config import settings
from app.models.job import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("worker")


def main() -> None:
    queue_names = sys.argv[1:] if len(sys.argv) > 1 else ["default"]
    conn = Redis.from_url(settings.redis_url)

    logger.info("Starting RQ worker on queues: %s", queue_names)
    logger.info("Redis URL: %s", settings.redis_url)

    # Ensure DB tables exist (worker may start before uvicorn)
    init_db()

    queues = [Queue(name, connection=conn) for name in queue_names]
    worker = Worker(queues, connection=conn)
    worker.work()


if __name__ == "__main__":
    main()
