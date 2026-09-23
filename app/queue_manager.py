"""
Redis & RQ Queue Management for T-Pot Sensor Deployer.
Influenced by FragForge's dedicated worker and Redis queue architecture.

Alternative Redis Port:
The default Redis port 6379 is occupied on the Hive by a redishoneypot container.
This module binds by default to redis://127.0.0.1:6380/0.
"""

import json
import logging
import os
import time
from typing import Dict, List, Optional, Any
import redis
from rq import Queue, Worker

logger = logging.getLogger("tpot.queue")

DEFAULT_REDIS_URL = "redis://127.0.0.1:6380/0"
REDIS_URL = os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
QUEUE_NAME = "tpot-deployer-tasks"
TASK_TTL_SECONDS = 86400  # 24 hours log & meta retention in Redis

RECENT_TASKS_KEY = "deploy:recent_tasks"

# In-memory fallback stores for test suite or when Redis is offline
_FALLBACK_LOGS: Dict[str, List[dict]] = {}
_FALLBACK_META: Dict[str, dict] = {}
_FALLBACK_TASK_IDS: List[str] = []


def get_redis_connection() -> redis.Redis:
    """Create a Redis client instance configured with the alternative port."""
    url = os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
    # decode_responses=False is required by RQ for job serialization
    return redis.from_url(url, decode_responses=False, socket_timeout=3.0, socket_connect_timeout=3.0)


# Primary connection instance
redis_conn = get_redis_connection()
task_queue = Queue(QUEUE_NAME, connection=redis_conn)


def is_redis_available() -> bool:
    """Check if the dedicated Redis service is reachable."""
    try:
        return bool(redis_conn.ping())
    except Exception:
        return False


def get_redis_info() -> dict:
    """Get status information about the dedicated Redis server."""
    try:
        info = redis_conn.info()
        def _to_str(val):
            return val.decode("utf-8") if isinstance(val, bytes) else val
        return {
            "available": True,
            "url": REDIS_URL,
            "version": _to_str(info.get("redis_version", "unknown")),
            "used_memory_human": _to_str(info.get("used_memory_human", "unknown")),
            "connected_clients": info.get("connected_clients", 0),
            "uptime_in_days": info.get("uptime_in_days", 0)
        }
    except Exception as e:
        return {
            "available": False,
            "url": REDIS_URL,
            "error": str(e)
        }


def record_task(
    task_id: str,
    task_type: str,
    type_label: str,
    sensor_name: str,
    sensor_type: str,
    status: str = "queued",
    meta_extra: Optional[dict] = None
) -> dict:
    """Register a new task with high-level metadata and push to recent task list."""
    now_ts = time.time()
    meta = {
        "id": task_id,
        "task_type": task_type,
        "type_label": type_label,
        "sensor_name": sensor_name,
        "sensor_type": sensor_type,
        "status": status,
        "percent": "0",
        "last_message": "Task queued in worker engine",
        "created_at": str(now_ts),
        "updated_at": str(now_ts),
        "completed_at": "",
        "deployed": "[]",
        "error": ""
    }
    if meta_extra:
        for k, v in meta_extra.items():
            meta[k] = json.dumps(v) if isinstance(v, (dict, list)) else str(v)

    try:
        meta_key = f"deploy:{task_id}:meta"
        redis_conn.hset(meta_key, mapping=meta)
        redis_conn.expire(meta_key, TASK_TTL_SECONDS)

        # Prepend to recent tasks list, de-duplicating and limiting to 100
        redis_conn.lrem(RECENT_TASKS_KEY, 0, task_id)
        redis_conn.lpush(RECENT_TASKS_KEY, task_id)
        redis_conn.ltrim(RECENT_TASKS_KEY, 0, 99)
        redis_conn.expire(RECENT_TASKS_KEY, TASK_TTL_SECONDS)
    except Exception as e:
        logger.debug(f"Redis record_task fallback for {task_id}: {e}")
        _FALLBACK_META[task_id] = meta
        if task_id in _FALLBACK_TASK_IDS:
            _FALLBACK_TASK_IDS.remove(task_id)
        _FALLBACK_TASK_IDS.insert(0, task_id)

    return meta


def publish_task_log(
    task_id: str,
    message: str,
    step: Optional[int] = None,
    percent: Optional[int] = None,
    level: str = "info"
) -> dict:
    """
    Publish a log event for a deployment or maintenance task.
    Pushes to Redis list for history replay and Pub/Sub channel for live SSE streaming.
    """
    evt = {
        "message": message,
        "step": step,
        "percent": percent,
        "level": level,
        "timestamp": time.time()
    }
    payload_str = json.dumps(evt)

    try:
        log_key = f"deploy:{task_id}:logs"
        stream_channel = f"deploy:{task_id}:stream"

        # Append to historical log list
        redis_conn.rpush(log_key, payload_str)
        redis_conn.expire(log_key, TASK_TTL_SECONDS)

        # Broadcast event to active SSE subscribers
        redis_conn.publish(stream_channel, payload_str)

        # Update metadata progress in Redis hash
        meta_key = f"deploy:{task_id}:meta"
        updates = {"updated_at": str(time.time()), "last_message": message}
        if percent is not None:
            updates["percent"] = str(percent)
        # Transition queued -> running once worker logs start
        curr_status = redis_conn.hget(meta_key, "status")
        if curr_status:
            status_str = curr_status.decode("utf-8") if isinstance(curr_status, bytes) else str(curr_status)
            if status_str == "queued":
                updates["status"] = "running"
        redis_conn.hset(meta_key, mapping=updates)
    except Exception as e:
        logger.debug(f"Redis log publish fallback for {task_id}: {e}")
        # In-memory fallback
        if task_id not in _FALLBACK_LOGS:
            _FALLBACK_LOGS[task_id] = []
        _FALLBACK_LOGS[task_id].append(evt)
        if task_id in _FALLBACK_META:
            _FALLBACK_META[task_id]["updated_at"] = str(time.time())
            _FALLBACK_META[task_id]["last_message"] = message
            if percent is not None:
                _FALLBACK_META[task_id]["percent"] = str(percent)
            if _FALLBACK_META[task_id].get("status") == "queued":
                _FALLBACK_META[task_id]["status"] = "running"

    return evt


def set_task_status(
    task_id: str,
    status: str,
    deployed: Optional[List[dict]] = None,
    error: Optional[str] = None
) -> dict:
    """
    Update task execution state in Redis and broadcast completion event.
    """
    meta_key = f"deploy:{task_id}:meta"
    stream_channel = f"deploy:{task_id}:stream"
    now_ts = time.time()

    updates = {
        "status": status,
        "updated_at": str(now_ts),
    }
    if deployed is not None:
        updates["deployed"] = json.dumps(deployed)
    if error is not None:
        updates["error"] = error
    if status in ("completed", "failed"):
        updates["completed_at"] = str(now_ts)
        if status == "completed":
            updates["percent"] = "100"

    try:
        redis_conn.hset(meta_key, mapping=updates)
        redis_conn.expire(meta_key, TASK_TTL_SECONDS)

        # Broadcast a completion message to SSE subscribers, but only for terminal states. Intermediate
        # updates (e.g. "running" at job start) must not look like the end of the task to a live stream.
        if status in ("completed", "failed"):
            complete_evt = {
                "done": True,
                "status": status,
                "deployed": deployed or [],
                "error": error
            }
            redis_conn.publish(stream_channel, json.dumps(complete_evt))
    except Exception as e:
        logger.debug(f"Redis meta fallback for {task_id}: {e}")
        if task_id in _FALLBACK_META:
            _FALLBACK_META[task_id].update(updates)
        else:
            _FALLBACK_META[task_id] = {
                "id": task_id,
                "status": status,
                "deployed": json.dumps(deployed or []),
                "error": error or "",
                "updated_at": str(now_ts)
            }

    return get_task_status(task_id)


def update_task_meta(task_id: str, **fields) -> None:
    """Overwrite display fields on an existing task (e.g. sensor_name once a campaign cycle has
    generated its droplet name). Never touches status; use set_task_status for that."""
    updates = {k: str(v) for k, v in fields.items() if v is not None}
    if not updates:
        return
    try:
        redis_conn.hset(f"deploy:{task_id}:meta", mapping=updates)
    except Exception as e:
        logger.debug(f"Redis meta update fallback for {task_id}: {e}")
        if task_id in _FALLBACK_META:
            _FALLBACK_META[task_id].update(updates)


def get_task_logs(task_id: str) -> List[dict]:
    """Retrieve all historical logs for a given task."""
    try:
        log_key = f"deploy:{task_id}:logs"
        raw_items = redis_conn.lrange(log_key, 0, -1)
        if raw_items:
            logs = []
            for item in raw_items:
                try:
                    if isinstance(item, bytes):
                        item = item.decode("utf-8")
                    logs.append(json.loads(item))
                except Exception:
                    pass
            return logs
    except Exception:
        pass
    return _FALLBACK_LOGS.get(task_id, [])


def get_task_status(task_id: str) -> dict:
    """Retrieve current metadata and status for a task."""
    try:
        meta_key = f"deploy:{task_id}:meta"
        raw_meta = redis_conn.hgetall(meta_key)
        if raw_meta:
            decoded = {
                (k.decode("utf-8") if isinstance(k, bytes) else k):
                (v.decode("utf-8") if isinstance(v, bytes) else v)
                for k, v in raw_meta.items()
            }
            deployed_raw = decoded.get("deployed", "[]")
            try:
                deployed = json.loads(deployed_raw)
            except Exception:
                deployed = []

            try:
                percent = int(decoded.get("percent", 0))
            except Exception:
                percent = 0

            return {
                "id": task_id,
                "task_type": decoded.get("task_type", "do_deploy"),
                "type_label": decoded.get("type_label", "Deployment Task"),
                "sensor_name": decoded.get("sensor_name", "sensor"),
                "sensor_type": decoded.get("sensor_type", "cowrie"),
                "status": decoded.get("status", "unknown"),
                "percent": percent,
                "last_message": decoded.get("last_message", ""),
                "created_at": decoded.get("created_at", ""),
                "updated_at": decoded.get("updated_at", ""),
                "completed_at": decoded.get("completed_at", ""),
                "deployed": deployed,
                "error": decoded.get("error") or None,
                # Set only on campaign tasks (see scheduler_manager._task_begin); empty for manual ones.
                "campaign_id": decoded.get("campaign_id") or None,
                "campaign_name": decoded.get("campaign_name") or None,
                "cycle": decoded.get("cycle") or None,
            }
    except Exception:
        pass
    meta = dict(_FALLBACK_META.get(task_id, {"id": task_id, "status": "unknown", "deployed": [], "error": None}))
    # The fallback stores what the Redis hash would (JSON strings); return the same shapes as above.
    if isinstance(meta.get("deployed"), str):
        try:
            meta["deployed"] = json.loads(meta["deployed"])
        except Exception:
            meta["deployed"] = []
    meta["error"] = meta.get("error") or None
    return meta


def get_recent_tasks(limit: int = 25) -> List[dict]:
    """Retrieve a list of recent deployment and maintenance tasks with telemetry."""
    tasks = []
    seen = set()
    try:
        raw_ids = redis_conn.lrange(RECENT_TASKS_KEY, 0, limit - 1)
        for tid in raw_ids:
            if isinstance(tid, bytes):
                tid = tid.decode("utf-8")
            if tid not in seen:
                seen.add(tid)
                tasks.append(get_task_status(tid))
    except Exception as e:
        logger.debug(f"Redis get_recent_tasks fallback: {e}")
        for tid in _FALLBACK_TASK_IDS[:limit]:
            if tid not in seen:
                seen.add(tid)
                tasks.append(get_task_status(tid))
    return tasks


def reap_interrupted_tasks() -> int:
    """
    Call once at worker startup, before taking jobs: any task still marked 'running' belonged to a
    previous worker process that died (restart, rebuild, crash) and will never finish, so mark it failed
    instead of leaving a phantom 'running' task. Assumes a single worker (as in docker-compose.yml).
    """
    reaped = 0
    for t in get_recent_tasks(limit=100):
        # Campaign tasks are not tied to this process's lifetime: a cycle waiting for its droplet's IP
        # carries on after a restart, and one killed mid-Ansible is aborted (and its task failed) by the
        # scheduler itself. SchedulerManager.reap_orphaned_campaign_tasks handles the ones nothing owns.
        if str(t.get("task_type", "")).startswith("campaign_"):
            continue
        if t.get("status") == "running":
            msg = "Interrupted: the worker was restarted while this task was running"
            publish_task_log(t["id"], f"❌ {msg}", level="error")
            set_task_status(t["id"], "failed", error=msg)
            reaped += 1
    return reaped


def list_active_workers() -> List[dict]:
    """List all registered and running RQ worker processes."""
    try:
        workers = Worker.all(connection=redis_conn)
        res = []
        for w in sorted(workers, key=lambda x: x.name):
            current_job = w.get_current_job()
            res.append({
                "name": w.name,
                "hostname": getattr(w, "hostname", ""),
                "pid": getattr(w, "pid", None),
                "state": w.get_state(),
                "queues": [q.name for q in w.queues],
                "successful_job_count": getattr(w, "successful_job_count", 0),
                "failed_job_count": getattr(w, "failed_job_count", 0),
                "birth_date": w.birth_date.isoformat() if getattr(w, "birth_date", None) else None,
                "last_heartbeat": w.last_heartbeat.isoformat() if getattr(w, "last_heartbeat", None) else None,
                "current_job_id": current_job.id if current_job else None
            })
        return res
    except Exception as e:
        logger.debug(f"Failed to query active workers: {e}")
        return []


def get_queue_stats() -> dict:
    """Get queue depth and health metrics."""
    try:
        return {
            "queue_name": QUEUE_NAME,
            "pending_count": task_queue.count,
            "failed_count": task_queue.failed_job_registry.count,
            "started_count": task_queue.started_job_registry.count,
            "finished_count": task_queue.finished_job_registry.count
        }
    except Exception as e:
        return {
            "queue_name": QUEUE_NAME,
            "pending_count": 0,
            "error": str(e)
        }
