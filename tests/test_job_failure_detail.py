"""A crashed job keeps what actually went wrong (admins only), so a failure on a server whose logs
nobody can read can still be traced — found while chasing a "Make one like this" crash on staging."""
import asyncio

from mongomock_motor import AsyncMongoMockClient

from app.jobs import queue, worker


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_a_crash_is_recorded_with_its_cause_and_shown_to_admins_only():
    db = AsyncMongoMockClient()["t"]

    async def boom(db, job, ctx):
        raise KeyError("draft_id")

    worker._handlers["test_boom"] = boom
    try:
        _run(queue.enqueue(db, "test_boom", {}, brand_id="b1", max_attempts=1))
        job = _run(queue.claim(db, ["test_boom"]))
        _run(worker.run_one(db, job))
    finally:
        worker._handlers.pop("test_boom", None)

    stored = _run(queue.get(db, job["job_id"]))
    assert stored["status"] == queue.FAILED
    assert stored["error"]["message"].startswith("Something went wrong")      # what the person sees
    assert "KeyError('draft_id')" in stored["failures"][0]["detail"]          # what admins see
    assert "test_job_failure_detail.py" in stored["failures"][0]["detail"]
    assert "failures" not in queue.public_view(stored)
    recent = _run(queue.recent_failures(db))
    assert recent[0]["job_id"] == job["job_id"]
