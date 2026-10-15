import os
import time
import tempfile
import pytest
from pathlib import Path
from backend.app.storage import Storage, RequestRecord, ActionRecord
from backend.app.queue import DurableJobQueue, JobRecord
from backend.app.audio import AudioService
from backend.app.agent import AgentRunner
from backend.app.schemas import RunRequest
from backend.app.providers.base import BaseModelProvider, ModelResponse


@pytest.fixture
def temp_storage(tmp_path):
    db_file = tmp_path / "test_retention.db"
    store = Storage(db_url=f"sqlite:///{db_file}")
    yield store


@pytest.fixture
def temp_queue(tmp_path):
    db_file = tmp_path / "test_retention_queue.db"
    q = DurableJobQueue(db_url=f"sqlite:///{db_file}")
    yield q


@pytest.mark.asyncio
async def test_retention_cleanup_purges_old_data_and_expired_actions(temp_storage):
    now = time.time()
    old_time = now - (40 * 86400)  # 40 days ago

    # Insert old request and recent request
    old_req = RequestRecord(
        request_id="req-old",
        update_id=101,
        user_id=1,
        chat_id=1,
        message_id=1,
        text="old text",
        created_at=old_time,
        updated_at=old_time,
    )
    new_req = RequestRecord(
        request_id="req-new",
        update_id=102,
        user_id=1,
        chat_id=1,
        message_id=2,
        text="new text",
        created_at=now,
        updated_at=now,
    )
    await temp_storage.save_request(old_req)
    await temp_storage.save_request(new_req)

    # Insert old chat message and recent chat message
    await temp_storage.append_chat_message(1, "user", "old message")
    # Manually backdate the first chat message
    conn = temp_storage._get_connection()
    try:
        with conn:
            conn.execute("UPDATE chat_history SET created_at = ? WHERE content = 'old message'", (old_time,))
    finally:
        conn.close()
    await temp_storage.append_chat_message(1, "user", "recent message")

    # Insert expired action and active action
    expired_action = ActionRecord(
        action_id="act-exp",
        idempotency_key="idemp-exp",
        chat_id=1,
        user_id=1,
        tool_name="tool_a",
        arguments={},
        confirmation_token="tok1",
        status="pending",
        created_at=old_time,
        expires_at=now - 100,  # expired
    )
    active_action = ActionRecord(
        action_id="act-act",
        idempotency_key="idemp-act",
        chat_id=1,
        user_id=1,
        tool_name="tool_b",
        arguments={},
        confirmation_token="tok2",
        status="pending",
        created_at=now,
        expires_at=now + 300,  # active
    )
    await temp_storage.save_write_action(expired_action)
    await temp_storage.save_write_action(active_action)

    # Run retention cleanup (30 days)
    res = await temp_storage.cleanup_retention_data(history_days=30, request_days=30)
    assert res["deleted_history"] == 1
    assert res["deleted_requests"] == 1
    assert res["deleted_expired_actions"] == 1

    # Verify preserved records
    assert await temp_storage.get_by_update_id(101) is None
    assert await temp_storage.get_by_update_id(102) is not None

    hist = await temp_storage.get_chat_history(1)
    assert len(hist) == 1
    assert hist[0]["content"] == "recent message"

    assert await temp_storage.get_action_by_token_or_id(1, "tok1") is None
    assert await temp_storage.get_action_by_token_or_id(1, "tok2") is not None


@pytest.mark.asyncio
async def test_job_retention_cleanup(temp_queue):
    now = time.time()
    old_time = now - (30 * 3600)  # 30 hours ago

    # Create old completed job
    old_job = JobRecord(
        job_id="job-old",
        update_id=201,
        request_id="req-201",
        user_id=1,
        chat_id=1,
        message_id=1,
        text="task old",
        status="completed",
        updated_at=old_time,
    )
    # Create recent completed job
    recent_job = JobRecord(
        job_id="job-recent",
        update_id=202,
        request_id="req-202",
        user_id=1,
        chat_id=1,
        message_id=2,
        text="task recent",
        status="completed",
        updated_at=now,
    )
    # Create active pending job
    pending_job = JobRecord(
        job_id="job-pending",
        update_id=203,
        request_id="req-203",
        user_id=1,
        chat_id=1,
        message_id=3,
        text="task pending",
        status="pending",
        updated_at=old_time,
    )
    await temp_queue.enqueue(old_job)
    await temp_queue.enqueue(recent_job)
    await temp_queue.enqueue(pending_job)

    # Mark old_job as completed and backdate updated_at
    await temp_queue.complete_job("job-old", "done", None, True)
    conn = temp_queue._get_connection()
    try:
        with conn:
            conn.execute("UPDATE durable_jobs SET updated_at = ? WHERE job_id = 'job-old'", (old_time,))
    finally:
        conn.close()

    # Clean up jobs older than 24 hours
    deleted = await temp_queue.cleanup_job_retention(retention_hours=24.0)
    assert deleted == 1

    assert await temp_queue.get_job("job-old") is None
    assert await temp_queue.get_job("job-recent") is not None
    assert await temp_queue.get_job("job-pending") is not None


def test_delete_temporary_media_after_processing():
    with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as f:
        f.write(b"fake audio data")
        temp_path = f.name

    assert os.path.exists(temp_path)
    AudioService.cleanup_temp_media(temp_path)
    assert not os.path.exists(temp_path)


@pytest.mark.asyncio
async def test_usage_cap_enforcement(temp_storage, monkeypatch):
    # Set limit of 2 requests
    allowed1, _ = await temp_storage.check_and_increment_usage(user_id=888, max_requests=2)
    assert allowed1 is True

    allowed2, _ = await temp_storage.check_and_increment_usage(user_id=888, max_requests=2)
    assert allowed2 is True

    # 3rd request must be blocked
    allowed3, msg = await temp_storage.check_and_increment_usage(user_id=888, max_requests=2)
    assert allowed3 is False
    assert "Daily request cap" in msg


@pytest.mark.asyncio
async def test_failure_alert_trigger(temp_storage):
    # 1st failure: no alert
    alert1, count1 = await temp_storage.record_failure_and_check_alert(chat_id=555, error_code="ERR1", threshold=3)
    assert alert1 is False
    assert count1 == 1

    # 2nd failure: no alert
    alert2, count2 = await temp_storage.record_failure_and_check_alert(chat_id=555, error_code="ERR2", threshold=3)
    assert alert2 is False
    assert count2 == 2

    # 3rd failure: alert triggered
    alert3, count3 = await temp_storage.record_failure_and_check_alert(chat_id=555, error_code="ERR3", threshold=3)
    assert alert3 is True
    assert count3 == 3

    # Reset on success
    await temp_storage.reset_failure_counter(chat_id=555)

    # Next failure starts fresh
    alert_fresh, count_fresh = await temp_storage.record_failure_and_check_alert(chat_id=555, error_code="ERR4", threshold=3)
    assert alert_fresh is False
    assert count_fresh == 1
