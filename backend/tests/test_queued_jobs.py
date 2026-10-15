import pytest
import tempfile
import os
import time
import json
import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.schemas import JobRequest, JobResponse, RunRequest
from backend.app.queue import (
    DurableJobQueue,
    JobRecord,
    QueueWorker,
    compute_callback_hmac,
)
from backend.app.agent import AgentRunner
from backend.app.providers.base import BaseModelProvider, ModelResponse


class FakeJobModelProvider(BaseModelProvider):
    async def generate_response(self, messages, tools=None, system_prompt=""):
        return ModelResponse(text="Queued job background execution complete.")


@pytest.fixture
def temp_job_queue():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    queue = DurableJobQueue(db_url=f"sqlite:///{db_path}")
    yield queue
    try:
        os.remove(db_path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_enqueue_and_deduplication(temp_job_queue):
    """
    POST /v1/assistant/jobs durably records and queues the job.
    Replaying the same update_id reuses the existing job.
    """
    job1 = JobRecord(
        job_id="job_001",
        update_id=5001,
        request_id="tg:5001",
        user_id=12345,
        chat_id=12345,
        message_id=1,
        text="Long-running query 1",
    )
    enqueued1 = await temp_job_queue.enqueue(job1)
    assert enqueued1.job_id == "job_001"
    assert enqueued1.status == "queued"

    # Duplicate update_id returns existing job
    job2 = JobRecord(
        job_id="job_002",
        update_id=5001,  # same update_id
        request_id="tg:5001",
        user_id=12345,
        chat_id=12345,
        message_id=2,
        text="Different text, same update_id",
    )
    enqueued2 = await temp_job_queue.enqueue(job2)
    assert enqueued2.job_id == "job_001"


@pytest.mark.asyncio
async def test_worker_restart_resumes_or_safely_retries_job(temp_job_queue):
    """
    Complete when: A worker restart resumes or safely retries a job.
    If a worker crashed while job was running, expired lease allows restart to claim and complete it.
    """
    now = time.time()
    # Simulate a crashed worker whose lease expired 10 seconds ago
    crashed_job = JobRecord(
        job_id="job_crashed",
        update_id=5002,
        request_id="tg:5002",
        user_id=12345,
        chat_id=12345,
        message_id=3,
        text="Resume interrupted job",
        status="running",
        attempts=1,
        max_retries=3,
        leased_until=now - 10.0,  # expired lease
    )
    conn = temp_job_queue._get_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO durable_jobs (
                job_id, update_id, request_id, user_id, chat_id, message_id,
                text, status, attempts, max_retries, leased_until, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                crashed_job.job_id,
                crashed_job.update_id,
                crashed_job.request_id,
                crashed_job.user_id,
                crashed_job.chat_id,
                crashed_job.message_id,
                crashed_job.text,
                crashed_job.status,
                crashed_job.attempts,
                crashed_job.max_retries,
                crashed_job.leased_until,
                now - 30.0,
                now - 10.0,
            ),
        )
    conn.close()

    # New worker starts up after crash
    resumed = await temp_job_queue.lease_next_job(lease_seconds=60.0)
    assert resumed is not None
    assert resumed.job_id == "job_crashed"
    assert resumed.attempts == 2
    assert resumed.status == "running"
    assert resumed.leased_until > time.time()


@pytest.mark.asyncio
async def test_queue_worker_execution_and_signed_callback_delivery(temp_job_queue):
    """
    Queue worker runs model engine and sends HMAC-signed callback.
    Delivery limitation: tool execution is separate from delivery retries.
    """
    job = JobRecord(
        job_id="job_exec_001",
        update_id=5003,
        request_id="tg:5003",
        user_id=12345,
        chat_id=12345,
        message_id=4,
        text="Calculate fiscal projection",
        callback_url="http://mock-edge:8080/v1/assistant/callback",
    )
    await temp_job_queue.enqueue(job)

    runner = AgentRunner(provider=FakeJobModelProvider())
    worker = QueueWorker(queue=temp_job_queue, runner=runner, secret="test-secret-456")

    # Mock callback POST delivery
    received_requests = []

    async def fake_post(url, content=None, headers=None):
        received_requests.append({"url": url, "content": content, "headers": headers})
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        return mock_resp

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        leased = await temp_job_queue.lease_next_job()
        assert leased is not None
        await worker.process_job(leased)

    # Verify job marked completed in durable storage
    completed = await temp_job_queue.get_job("job_exec_001")
    assert completed is not None
    assert completed.status == "completed"
    assert completed.answer == "Queued job background execution complete."
    assert completed.callback_status == "delivered"

    # Verify callback payload and HMAC signature
    assert len(received_requests) == 1
    req_sent = received_requests[0]
    payload = json.loads(req_sent["content"].decode("utf-8"))
    assert payload["job_id"] == "job_exec_001"
    assert payload["status"] == "completed"
    assert payload["answer"] == "Queued job background execution complete."

    sig_header = req_sent["headers"]["X-Assistant-Signature"]
    assert "t=" in sig_header and "v1=" in sig_header
    parts = dict(p.split("=") for p in sig_header.split(","))
    ts = int(parts["t"])
    expected_hmac = compute_callback_hmac(req_sent["content"], ts, "test-secret-456")
    assert parts["v1"] == expected_hmac


def test_api_post_jobs_returns_202_with_job_id(monkeypatch):
    """
    Test FastAPI POST /v1/assistant/jobs endpoint:
    Returns 202 Accepted with job_id and status=queued.
    """
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-secret-token"}

    # Set ALLOWED_USER_ID
    monkeypatch.setenv("ALLOWED_USER_ID", "12345")
    monkeypatch.setenv("EDGE_BACKEND_SECRET", "test-secret-token")

    body = {
        "request_id": "tg:9999",
        "update_id": 9999,
        "user_id": 12345,
        "chat_id": 12345,
        "message_id": 10,
        "text": "Please run a long database export",
    }

    response = client.post("/v1/assistant/jobs", json=body, headers=headers)
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["job_id"].startswith("job_")
    assert data["status"] == "queued"
    assert data["request_id"] == "tg:9999"

    # GET /v1/assistant/jobs/{job_id}
    job_id = data["job_id"]
    get_res = client.get(f"/v1/assistant/jobs/{job_id}", headers=headers)
    assert get_res.status_code == 200
    assert get_res.json()["job_id"] == job_id
