"""
Automated unit & integration tests for T-Pot Deployer Dedicated Worker & Redis Queue.
Verifies queue manager, task log pub/sub and list persistence, job execution, and tag safeguards.
"""

# Isolate from real state: must run before any app module is imported.
import os as _os, sys as _sys, tempfile as _tempfile
from pathlib import Path as _Path
_os.environ["DATA_DIR"] = _tempfile.mkdtemp(prefix="tpot-test-")
# The T-Pot Hive is LIVE config (lswebpasswd + .env): tests must never resolve to the real one.
_os.environ["TPOT_DIR"] = _tempfile.mkdtemp(prefix="tpot-test-hive-")
_os.environ["SECRETS_DIR"] = _tempfile.mkdtemp(prefix="tpot-test-secrets-")
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import sys
import uuid
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.queue_manager import (
    redis_conn,
    task_queue,
    publish_task_log,
    set_task_status,
    get_task_logs,
    get_task_status,
    is_redis_available,
    list_active_workers,
    get_queue_stats,
    get_redis_info,
    REDIS_URL
)
from app.jobs import campaign_scheduler_tick_job, ttl_sweep_job
from rq import Worker


def test_redis_connection():
    print("=== Testing Redis Connection ===")
    assert is_redis_available(), "Redis should be available"
    info = get_redis_info()
    assert info.get("available") is True, "Redis info must indicate available"
    print(f"  ✓ Redis connected at {info.get('url')} (version: {info.get('version')})")


def test_task_status_and_logs():
    print("=== Testing Redis Log Persistence & Status Tracking ===")
    test_id = f"test-{uuid.uuid4().hex[:8]}"

    # Set initial status
    set_task_status(test_id, "queued")
    st = get_task_status(test_id)
    assert st.get("status") == "queued", f"Expected queued, got {st.get('status')}"
    print("  ✓ Task initial status set and retrieved as 'queued'")

    # Publish log messages
    publish_task_log(test_id, "Starting test step 1", step=1, percent=25)
    publish_task_log(test_id, "Running test step 2", step=2, percent=50)
    publish_task_log(test_id, "Completed test step 3", step=3, percent=100)

    # Retrieve logs (historical replay check)
    logs = get_task_logs(test_id)
    assert len(logs) == 3, f"Expected 3 logs, got {len(logs)}"
    assert logs[0]["message"] == "Starting test step 1"
    assert logs[0]["step"] == 1
    assert logs[0]["percent"] == 25
    assert logs[2]["percent"] == 100
    print(f"  ✓ Successfully verified {len(logs)} log entries with metadata (step/percent/timestamp)")

    # Set completed status with payload
    set_task_status(test_id, "completed", deployed=[{"id": 12345, "name": "test-sensor"}])
    st_done = get_task_status(test_id)
    assert st_done.get("status") == "completed"
    assert st_done.get("deployed") == [{"id": 12345, "name": "test-sensor"}]
    print("  ✓ Task completed status and JSON payload verified")


def test_rq_job_dispatch_and_execution():
    print("=== Testing RQ Job Enqueue & Execution ===")
    job = task_queue.enqueue(campaign_scheduler_tick_job)
    assert job is not None, "Enqueued job should not be None"
    print(f"  ✓ Enqueued campaign_scheduler_tick_job with job_id: {job.id}")

    # If background worker didn't pick it up yet, burst worker will process it
    import time
    for _ in range(20):
        job.refresh()
        status = job.get_status()
        if status == "finished":
            break
        elif status == "queued":
            worker = Worker([task_queue], connection=redis_conn)
            worker.work(burst=True)
        time.sleep(0.2)

    job.refresh()
    assert job.get_status() == "finished", f"Expected finished, got {job.get_status()}"
    print(f"  ✓ Job executed by RQ worker with status: {job.get_status()}")



def test_active_workers_and_queue_stats():
    print("=== Testing Worker Discovery & Queue Statistics ===")
    stats = get_queue_stats()
    assert stats["queue_name"] == "tpot-deployer-tasks"
    print(f"  ✓ Queue stats retrieved: {stats}")

    workers = list_active_workers()
    print(f"  ✓ Discovered {len(workers)} active RQ worker(s) registered in Redis")
    if workers:
        for w in workers:
            print(f"    - Worker {w['name'][:8]} on {w['hostname']} (PID {w['pid']}, status: {w['state']})")


def test_do_tag_auto_creation_logic():
    print("=== Testing DO Client Tag Auto-Creation Logic ===")
    from app.do_client import DOClient, DOAPIError
    from app.config import get_do_token

    # 1. Test unit behavior when tag already exists (422 response)
    client = DOClient(token="mock-token")
    def mock_request(method, path, **kwargs):
        if method == "POST" and path == "/tags":
            raise DOAPIError("Tag already exists", status_code=422)
        if method == "GET" and path == "/tags/already-existing-tag":
            return {"tag": {"name": "already-existing-tag"}}
        return {}
    client._request = mock_request

    tag_result = client.ensure_tag("already-existing-tag")
    assert tag_result.get("name") == "already-existing-tag", f"Expected tag name, got {tag_result}"
    print("  ✓ Idempotent tag creation verified for existing tag (handled 422 gracefully)")

    # 2. Test new tag creation response
    client_new = DOClient(token="mock-token")
    client_new._request = lambda method, path, **kwargs: {"tag": {"name": "brand-new-sensor-tag"}}
    res_new = client_new.ensure_tag("brand-new-sensor-tag")
    assert res_new.get("name") == "brand-new-sensor-tag"
    print("  ✓ Auto-creation of brand-new tag verified")

    # 3. Test ensure_tags list
    ensured = client_new.ensure_tags(["tag1", "tag2", "tag1"])
    assert len(ensured) == 2, f"Deduplication failed, got {len(ensured)}"
    print("  ✓ ensure_tags list batching and deduplication verified")



if __name__ == "__main__":
    try:
        test_redis_connection()
        test_task_status_and_logs()
        test_rq_job_dispatch_and_execution()
        test_active_workers_and_queue_stats()
        test_do_tag_auto_creation_logic()
        print("\n🎉 All dedicated worker & Redis queue tests PASSED!")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
