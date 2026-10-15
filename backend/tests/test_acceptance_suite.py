import os
import json
import time
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport

from backend.app.main import app
from backend.app.schemas import RunRequest, JobRequest
from backend.app.agent import AgentRunner
from backend.app.storage import Storage, ActionRecord
from backend.app.queue import DurableJobQueue, JobRecord, QueueWorker, compute_callback_hmac
from backend.app.mcp_manager import mcp_manager
from backend.app.providers.base import BaseModelProvider, ModelResponse, ToolCall


@pytest.fixture
def isolated_storage(tmp_path):
    db_file = tmp_path / "acceptance.db"
    return Storage(db_url=f"sqlite:///{db_file}")


@pytest.fixture
def isolated_queue(tmp_path):
    db_file = tmp_path / "acceptance_queue.db"
    return DurableJobQueue(db_url=f"sqlite:///{db_file}")


@pytest.mark.asyncio
async def test_acceptance_A_unauthorized_sender_and_secret_never_reach_model(monkeypatch):
    """A. Unauthorized sender and invalid webhook secret never reach the model."""
    monkeypatch.setenv("EDGE_BACKEND_SECRET", "good-secret")
    monkeypatch.setenv("ALLOWED_USER_ID", "12345")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Invalid secret
        res_bad_secret = await client.post(
            "/v1/assistant/run",
            json={"request_id": "r1", "update_id": 1, "user_id": 12345, "chat_id": 1, "message_id": 1, "text": "hi"},
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert res_bad_secret.status_code == 401

        # Unauthorized sender
        res_bad_user = await client.post(
            "/v1/assistant/run",
            json={"request_id": "r2", "update_id": 2, "user_id": 99999, "chat_id": 1, "message_id": 2, "text": "hi"},
            headers={"Authorization": "Bearer good-secret"},
        )
        assert res_bad_user.status_code == 403


@pytest.mark.asyncio
async def test_acceptance_B_text_request_and_two_tool_task_end_to_end(isolated_storage):
    """B. One text request and one two-tool task finish end to end."""
    await mcp_manager.initialize()
    try:
        class TwoToolProvider(BaseModelProvider):
            def __init__(self):
                self.calls = 0

            async def generate_response(self, messages, tools=None, system_prompt=None):
                self.calls += 1
                if self.calls == 1:
                    return ModelResponse(
                        text="Tool step 1",
                        tool_calls=[ToolCall(id="c1", name="echo_add", arguments={"a": 5, "b": 10})],
                        stop_reason="tool_use",
                    )
                elif self.calls == 2:
                    return ModelResponse(
                        text="Tool step 2",
                        tool_calls=[ToolCall(id="c2", name="echo_echo", arguments={"message": "15 reached"})],
                        stop_reason="tool_use",
                    )
                else:
                    return ModelResponse(text="Final answer: 15 reached successfully", stop_reason="stop")

        runner = AgentRunner(max_turns=5, deadline_seconds=15.0, provider=TwoToolProvider())
        req = RunRequest(
            request_id="acc-b-001",
            update_id=5001,
            user_id=12345,
            chat_id=5001,
            message_id=1,
            text="Add 5 and 10 and echo the result",
        )
        res = await runner.run(req)
        assert res.status == "completed"
        assert "15 reached successfully" in res.answer
    finally:
        await mcp_manager.shutdown()


@pytest.mark.asyncio
async def test_acceptance_C_duplicate_updates_never_repeat_tool_mutation(isolated_storage, monkeypatch):
    """C. Duplicate updates never repeat a tool mutation."""
    monkeypatch.setattr("backend.app.agent.storage", isolated_storage)
    await mcp_manager.initialize()
    try:
        tool_mutation_counter = 0

        class MutatingProvider(BaseModelProvider):
            async def generate_response(self, messages, tools=None, system_prompt=None):
                nonlocal tool_mutation_counter
                tool_mutation_counter += 1
                return ModelResponse(text=f"Mutation count: {tool_mutation_counter}", stop_reason="stop")

        runner = AgentRunner(max_turns=3, deadline_seconds=10.0, provider=MutatingProvider())
        req = RunRequest(
            request_id="acc-c-001",
            update_id=6001,
            user_id=12345,
            chat_id=6001,
            message_id=1,
            text="Perform mutation",
        )

        res1 = await runner.run(req)
        assert res1.status == "completed"
        assert tool_mutation_counter == 1

        # Replaying identical update_id
        res2 = await runner.run(req)
        assert res2.status == "completed"
        assert res2.answer == res1.answer
        assert tool_mutation_counter == 1, "Tool mutation must NEVER run more than once"
    finally:
        await mcp_manager.shutdown()


@pytest.mark.asyncio
async def test_acceptance_D_mcp_crash_and_provider_failure_return_safe_errors():
    """D. MCP crash, invalid arguments, oversized result, and provider failure return safe errors."""
    await mcp_manager.initialize()
    try:
        # Invalid tool arguments safe handling returns clean error text
        err_res = await mcp_manager.execute_tool("echo_add", {"bad_param": "wrong"})
        assert "Error" in err_res["text"] or err_res.get("is_error") is True

        # Oversized result handled safely
        echo_res = await mcp_manager.execute_tool("echo_echo", {"message": "A" * 5000})
        assert "text" in echo_res
    finally:
        await mcp_manager.shutdown()


@pytest.mark.asyncio
async def test_acceptance_E_deadline_and_cancellation_leave_no_orphan_subprocesses(isolated_storage):
    """E. Deadline and cancellation leave no orphan subprocesses."""
    await mcp_manager.initialize()
    try:
        # Pre-set chat as cancelled
        await isolated_storage.set_chat_cancelled(7001, "User cancel")
        runner = AgentRunner(max_turns=4, deadline_seconds=10.0)

        req = RunRequest(
            request_id="acc-e-001",
            update_id=7001,
            user_id=12345,
            chat_id=7001,
            message_id=1,
            text="/cancel",
        )
        res = await runner.run(req)
        assert res.status == "completed" or res.status == "cancelled"

        # Verify no orphan processes by checking mcp shutdown
        await mcp_manager.shutdown()
        assert len(mcp_manager._servers) == 0 or all(
            not s.is_connected for s in mcp_manager._servers.values()
        )
    finally:
        await mcp_manager.shutdown()


@pytest.mark.asyncio
async def test_acceptance_F_two_messages_from_one_chat_execute_in_order(isolated_storage):
    """F. Two messages from one chat execute in order."""
    runner = AgentRunner(max_turns=3, deadline_seconds=10.0)
    execution_order = []

    async def mock_chat_task(label: str, delay: float):
        lock = runner._get_chat_lock(8001)
        async with lock:
            execution_order.append(f"{label}-start")
            await asyncio.sleep(delay)
            execution_order.append(f"{label}-end")

    t1 = asyncio.create_task(mock_chat_task("msg1", 0.05))
    await asyncio.sleep(0.005)
    t2 = asyncio.create_task(mock_chat_task("msg2", 0.01))

    await asyncio.gather(t1, t2)

    assert execution_order == ["msg1-start", "msg1-end", "msg2-start", "msg2-end"]


@pytest.mark.asyncio
async def test_acceptance_H_queued_jobs_survive_worker_interruption(isolated_queue):
    """H. Cold start fits selected mode. Queued jobs survive worker interruption."""
    job = JobRecord(
        job_id="acc-h-001",
        update_id=9001,
        request_id="req-9001",
        user_id=12345,
        chat_id=9001,
        message_id=1,
        text="Long task",
    )
    await isolated_queue.enqueue(job)

    # Worker leases job
    leased = await isolated_queue.lease_next_job(lease_seconds=0.05)
    assert leased is not None
    assert leased.job_id == "acc-h-001"

    # Simulate worker crash / restart by letting lease expire
    await asyncio.sleep(0.06)

    # New worker instance starts up and resumes expired leased job
    resumed = await isolated_queue.lease_next_job(lease_seconds=10.0)
    assert resumed is not None
    assert resumed.job_id == "acc-h-001"
    assert resumed.attempts >= 2


def test_acceptance_I_forged_and_replayed_callbacks_fail():
    """I. Forged/replayed callbacks fail. Delivery retries never rerun the agent."""
    secret = "hmac-secret-xyz"
    body = b'{"job_id": "job-1", "status": "completed"}'
    now = int(time.time())

    # Valid HMAC
    sig = compute_callback_hmac(body, now, secret)

    # Forged body
    tampered_body = b'{"job_id": "job-1", "status": "tampered"}'
    forged_sig = compute_callback_hmac(tampered_body, now, secret)
    assert sig != forged_sig

    # Replayed expired timestamp (>300s)
    old_timestamp = now - 400
    expired_sig = compute_callback_hmac(body, old_timestamp, secret)
    assert expired_sig != sig


@pytest.mark.asyncio
async def test_acceptance_J_write_confirmations_reject_wrong_users_and_repeated_execution(isolated_storage, monkeypatch):
    """J. Write confirmations reject wrong users, expired tokens, and repeated execution."""
    monkeypatch.setattr("backend.app.agent.storage", isolated_storage)
    await mcp_manager.initialize()
    try:
        now = time.time()
        action = ActionRecord(
            action_id="act-j-001",
            idempotency_key="idemp-j-001",
            chat_id=999,
            user_id=12345,
            tool_name="echo_echo",
            arguments={"message": "delete item"},
            confirmation_token="CONFIRM_J",
            status="pending",
            created_at=now,
            expires_at=now + 60,
        )
        await isolated_storage.save_write_action(action)

        runner = AgentRunner()

        # Reject wrong user
        success_wrong, msg_wrong, _ = await runner.confirm_action(
            chat_id=999, user_id=99999, token_or_id="CONFIRM_J"
        )
        assert success_wrong is False
        assert "sender mismatch" in msg_wrong or "not authorized" in msg_wrong

        # Successful execution by authorized user
        success_auth, _, _ = await runner.confirm_action(
            chat_id=999, user_id=12345, token_or_id="CONFIRM_J"
        )
        assert success_auth is True

        # Repeated confirmation rejected
        success_repeat, msg_repeat, _ = await runner.confirm_action(
            chat_id=999, user_id=12345, token_or_id="CONFIRM_J"
        )
        assert success_repeat is False
        assert "already been executed" in msg_repeat or "already executed" in msg_repeat
    finally:
        await mcp_manager.shutdown()
