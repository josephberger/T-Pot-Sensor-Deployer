#!/usr/bin/env python3
"""
T-Pot Sensor Deployer - Dedicated RQ Worker Process.
Influenced by FragForge's dedicated worker pattern.

Listens to task queue on Redis (port 6380) and processes:
- DigitalOcean droplet provisioning & teardown
- GCP Terraform provisioning & teardown
- Periodic campaign schedule reconciliation
- Periodic TTL lease sweeps

Usage:
    python3 worker.py
"""

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from rq import Worker, Queue
from app.queue_manager import (
    redis_conn,
    task_queue,
    QUEUE_NAME,
    REDIS_URL,
    is_redis_available,
    reap_interrupted_tasks
)
from app.db import db_fail_interrupted_provisioning
from app.jobs import campaign_scheduler_tick_job, ttl_sweep_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("tpot.worker")


class SchedulerThread(threading.Thread):
    """
    Background scheduler ticker thread running inside the worker process.
    Keeps recurring maintenance (TTL sweeps and campaign rotations) out of the web server.
    """

    def __init__(self, tick_interval: float = 10.0, ttl_interval: float = 60.0):
        super().__init__(daemon=True, name="WorkerSchedulerThread")
        self.tick_interval = tick_interval
        self.ttl_interval = ttl_interval
        self.running = True
        self._last_ttl_check = 0.0

    def run(self):
        logger.info(f"Worker scheduler ticker loop started (reconcile={self.tick_interval}s, ttl={self.ttl_interval}s).")
        while self.running:
            try:
                # 1. Campaign scheduler tick
                campaign_scheduler_tick_job()

                # 2. Periodic TTL sweep
                now = time.time()
                if now - self._last_ttl_check >= self.ttl_interval:
                    ttl_sweep_job()
                    self._last_ttl_check = now

            except Exception as e:
                logger.warning(f"Error in scheduler worker loop: {e}")

            time.sleep(self.tick_interval)

    def stop(self):
        self.running = False


def main():
    logger.info("==========================================================")
    logger.info("  T-Pot Sensor Deployer - Dedicated RQ Worker Starting    ")
    logger.info("==========================================================")
    logger.info(f"Target Redis: {REDIS_URL}")
    logger.info(f"Active Queue: {QUEUE_NAME}")

    # Verify Redis connectivity before starting
    if not is_redis_available():
        logger.error(f"Cannot connect to Redis at {REDIS_URL}. Ensure the Redis container is running on port 6380.")
        sys.exit(1)

    logger.info("Connected to Redis successfully.")

    # Tasks/droplets left mid-flight by a previous worker process will never finish; mark them so.
    reaped = reap_interrupted_tasks()
    failed = db_fail_interrupted_provisioning()
    if reaped or failed:
        logger.warning(f"Marked {reaped} interrupted task(s) and {failed} half-provisioned sensor(s) as failed.")

    # Start the background periodic scheduler loop inside this worker process
    scheduler = SchedulerThread(tick_interval=10.0, ttl_interval=60.0)
    scheduler.start()

    # Create and run the RQ worker
    worker = Worker([task_queue], connection=redis_conn)

    def sig_handler(signum, frame):
        logger.info(f"Received termination signal ({signum}). Shutting down worker gracefully...")
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    logger.info(f"Worker '{worker.name}' ready and listening for jobs...")
    try:
        # with_scheduler=False because we run our dedicated SchedulerThread
        worker.work(with_scheduler=False)
    except KeyboardInterrupt:
        scheduler.stop()
        logger.info("Worker stopped by user.")


if __name__ == "__main__":
    main()
