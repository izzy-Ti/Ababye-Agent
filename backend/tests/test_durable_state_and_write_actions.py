import os
import time
import tempfile
import asyncio
import pytest
from unittest.mock import patch, MagicMock

from backend.app.storage import Storage, RequestRecord, ActionRecord
from backend.app.agent import AgentRunner
from backend.app.schemas import RunRequest
from backend.app.providers.base import BaseModelProvider, ModelResponse
from backend.app.mcp_manager import mcp_manager


@pytest.fixture
def test_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = tmp.name
    yield path
    try:
        os.remove(path)
    except Exception:
        pass


@pytest.mark.asyncio
async def test_container_restart_preserves_completed_requests(test_db_path):
    """
    Complete when: Container restart preserves completed requests.
    Verifies storage of request ID, update ID, user/chat IDs, status, timestamps,
    bounded history, answer, and error code across independent Storage instances.
    """
    storage1 = Storage(f"sqlite:///{test_db_path}")

    now = time.time()
    history_snapshot = [
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "4"},
    ]

    record = RequestRecord(
        request_id="tg:restart-100",
        update_id=1001,
        user_id=12345,
        chat_id=8888,
        message_id=42,
        text="Calculate total",
        status="completed",
        answer="Total calculated: 42",
        error_code=None,
        tool_calls_count=2,
        bounded_history=history_snapshot,
        created_at=now - 5,
        updated_at=now,
    )
    await storage1.save_request(record)

    # Simulate container restart: instantiate brand new storage instance pointing to the same file
    storage2 = Storage(f"sqlite:///{test_db_path}")
    loaded = await storage2.get_by_update_id(1001)

    assert loaded is not None
    assert loaded.request_id == "tg:restart-100"
    assert loaded.update_id == 1001
    assert loaded.user_id == 12345
    assert loaded.chat_id == 8888
    assert loaded.message_id == 42
    assert loaded.text == "Calculate total"
    assert loaded.status == "completed"
    assert loaded.answer == "Total calculated: 42"
    assert loaded.error_code is None
    assert loaded.tool_calls_count == 2
    assert len(loaded.bounded_history) == 2
    assert loaded.bounded_history[0]["content"] == "What is 2+2?"
    assert loaded.created_at == pytest.approx(now - 5, abs=0.1)
    assert loaded.updated_at == pytest.approx(now, abs=0.1)


@pytest.mark.asyncio
async def test_reset_command_clears_chat_context(test_db_path):
    """
    Verifies /reset clears chat context and returns confirmation.
    """
    storage = Storage(f"sqlite:///{test_db_path}")
    await storage.append_chat_message(5001, "user", "Hello")
    await storage.append_chat_message(5001, "assistant", "Hi there")

    # History has 2 messages
    history = await storage.get_chat_history(5001)
    assert len(history) == 2

    runner = AgentRunner()
    req = RunRequest(
        request_id="tg:reset-1",
        update_id=1002,
        user_id=12345,
        chat_id=5001,
        message_id=99,
        text="/reset",
    )

    with patch("backend.app.agent.storage", storage):
        res = await runner.run(req)

    assert res.status == "completed"
    assert "cleared" in res.answer.lower()

    # History is now empty
    history_after = await storage.get_chat_history(5001)
    assert len(history_after) == 0


@pytest.mark.asyncio
async def test_write_action_lifecycle_and_rejections(test_db_path):
    """
    Write actions, only after the read-only MVP:
    1. Represent a proposed action as a stored record with validated arguments and a short-lived confirmation token.
    2. Display the exact action in Telegram format.
    3. Verify sender, action ID, expiry, and unused state on confirmation.
    4. Execute once using an idempotency key.
    5. A model sentence expressing approval is insufficient.
    """
    storage = Storage(f"sqlite:///{test_db_path}")
    runner = AgentRunner()

    await mcp_manager.initialize()
    try:
        with patch("backend.app.agent.storage", storage):
            # 1. Propose write action
            action, display_text = await runner.propose_write_action(
                chat_id=7001,
                user_id=12345,
                tool_name="echo_echo",
                arguments={"message": "Critical write operation"},
                ttl_seconds=2.0,  # 2 second expiry for testing
            )

            assert action.action_id.startswith("act_")
            assert action.confirmation_token is not None
            assert action.idempotency_key is not None
            assert action.status == "pending"
            assert "Critical write operation" in display_text
            assert f"/confirm {action.confirmation_token}" in display_text

            # 2. Rejection Test: Wrong user tries to confirm (Acceptance Test J)
            success, msg, _ = await runner.confirm_action(
                chat_id=7001,
                user_id=99999,  # Unauthorized wrong user
                token_or_id=action.confirmation_token,
            )
            assert not success
            assert "sender mismatch" in msg.lower() or "not authorized" in msg.lower()

            # 3. Conversational text approval is INSUFFICIENT
            # Calling standard model loop with "I approve this action" does NOT execute the tool
            class ApprovingModel(BaseModelProvider):
                async def generate_response(self, messages, tools=None, system_prompt=None):
                    return ModelResponse(text="I approve the execution of this action.")

            conversational_runner = AgentRunner(provider=ApprovingModel())
            conv_req = RunRequest(
                request_id="tg:conv-1",
                update_id=1003,
                user_id=12345,
                chat_id=7001,
                message_id=5,
                text="Yes I approve and want you to run the action",
            )
            conv_res = await conversational_runner.run(conv_req)
            assert conv_res.status == "completed"
            # Verify action status is STILL pending (not executed!)
            check_action = await storage.get_write_action(action.action_id)
            assert check_action.status == "pending"
            assert check_action.executed_at is None

            # 4. Successful confirmation by authorized user
            success, msg, result = await runner.confirm_action(
                chat_id=7001,
                user_id=12345,  # Authorized user
                token_or_id=action.confirmation_token,
            )
            assert success
            assert "executed successfully" in msg.lower()
            assert result is not None
            assert result["text"] == "Echo: Critical write operation"

            # Verify action record updated to executed
            executed_action = await storage.get_write_action(action.action_id)
            assert executed_action.status == "executed"
            assert executed_action.executed_at is not None

            # 5. Rejection Test: Repeated execution rejected (Acceptance Test J - Idempotency)
            repeat_success, repeat_msg, _ = await runner.confirm_action(
                chat_id=7001,
                user_id=12345,
                token_or_id=action.confirmation_token,
            )
            assert not repeat_success
            assert "already been executed" in repeat_msg.lower() or "repeated execution blocked" in repeat_msg.lower()

            # 6. Rejection Test: Expired token rejected (Acceptance Test J)
            expired_action, _ = await runner.propose_write_action(
                chat_id=7001,
                user_id=12345,
                tool_name="echo_echo",
                arguments={"message": "Fast expire"},
                ttl_seconds=0.05,  # Expires in 50ms
            )
            await asyncio.sleep(0.08)  # Wait for expiration

            exp_success, exp_msg, _ = await runner.confirm_action(
                chat_id=7001,
                user_id=12345,
                token_or_id=expired_action.confirmation_token,
            )
            assert not exp_success
            assert "expired" in exp_msg.lower()

    finally:
        await mcp_manager.shutdown()


@pytest.mark.asyncio
async def test_confirm_via_chat_command(test_db_path):
    """
    Verifies that sending '/confirm <token>' through runner.run() executes the action.
    """
    storage = Storage(f"sqlite:///{test_db_path}")
    runner = AgentRunner()

    await mcp_manager.initialize()
    try:
        with patch("backend.app.agent.storage", storage):
            action, _ = await runner.propose_write_action(
                chat_id=8801,
                user_id=12345,
                tool_name="echo_add",
                arguments={"a": 25, "b": 17},
            )

            # User sends /confirm <token>
            req = RunRequest(
                request_id="tg:confirm-cmd",
                update_id=2001,
                user_id=12345,
                chat_id=8801,
                message_id=20,
                text=f"/confirm {action.confirmation_token}",
            )
            res = await runner.run(req)

            assert res.status == "completed"
            assert "executed successfully" in res.answer.lower()
            assert "42" in res.answer
    finally:
        await mcp_manager.shutdown()
