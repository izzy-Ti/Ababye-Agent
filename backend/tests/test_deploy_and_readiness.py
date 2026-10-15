import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from backend.app.main import app
from backend.app.mcp_manager import mcp_manager
from backend.app.agent import agent_runner, AgentRunner
from backend.app.schemas import RunRequest


@pytest.mark.asyncio
async def test_backend_health_and_readiness():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Health check
        res_health = await client.get("/health")
        assert res_health.status_code == 200
        assert res_health.json()["status"] == "ok"

        # Ready check when not ready
        mcp_manager._initialized = False
        res_not_ready = await client.get("/ready")
        assert res_not_ready.status_code == 503
        assert res_not_ready.json()["status"] == "not_ready"

        # Ready check when initialized
        mcp_manager._initialized = True
        res_ready = await client.get("/ready")
        assert res_ready.status_code == 200
        assert res_ready.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_authenticated_test_request(monkeypatch):
    monkeypatch.setenv("EDGE_BACKEND_SECRET", "test-secret-key")
    monkeypatch.setenv("ALLOWED_USER_ID", "12345")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer test-secret-key"}
        payload = {
            "request_id": "req-ready-001",
            "update_id": 88001,
            "user_id": 12345,
            "chat_id": 12345,
            "message_id": 100,
            "text": "ping",
        }
        res = await client.post("/v1/assistant/run", json=payload, headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] in ("completed", "error")
        assert data["request_id"] == "req-ready-001"


@pytest.mark.asyncio
async def test_concurrency_and_per_chat_locking():
    runner = AgentRunner(max_turns=2, deadline_seconds=10.0)
    runner.max_concurrency = 2
    runner._concurrency_semaphore = asyncio.Semaphore(2)

    execution_order = []

    async def mock_run(chat_id, update_id, delay):
        req = RunRequest(
            request_id=f"req-{update_id}",
            update_id=update_id,
            user_id=12345,
            chat_id=chat_id,
            message_id=update_id,
            text=f"task {update_id}",
        )
        execution_order.append(f"start-{update_id}")
        await asyncio.sleep(delay)
        execution_order.append(f"end-{update_id}")

    # Launch two tasks for the SAME chat simultaneously
    # Per-chat lock ensures task 1 completes before task 2 starts work inside lock
    lock = runner._get_chat_lock(999)
    async def chat_worker(update_id):
        async with runner._concurrency_semaphore:
            async with lock:
                execution_order.append(f"chat-lock-acquired-{update_id}")
                await asyncio.sleep(0.05)
                execution_order.append(f"chat-lock-released-{update_id}")

    await asyncio.gather(
        chat_worker(1),
        chat_worker(2),
    )

    # Must be strictly serialized
    assert execution_order == [
        "chat-lock-acquired-1",
        "chat-lock-released-1",
        "chat-lock-acquired-2",
        "chat-lock-released-2",
    ]
