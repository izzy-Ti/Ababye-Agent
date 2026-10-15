import pytest
import tempfile
import os
import asyncio
from unittest.mock import AsyncMock, patch

from backend.app.schemas import RunRequest, RunResponse
from backend.app.storage import Storage, ActionRecord
from backend.app.agent import AgentRunner
from backend.app.providers.base import BaseModelProvider, ModelResponse, ToolCall
from backend.app.mcp_manager import mcp_manager, MCPToolDefinition


class TwoStepToolProvider(BaseModelProvider):
    def __init__(self):
        self.turns = 0

    async def generate_response(self, messages, tools=None, system_prompt=""):
        self.turns += 1
        if self.turns == 1:
            return ModelResponse(
                text="Calling tool 1 and tool 2",
                tool_calls=[
                    ToolCall(id="c1", name="echo_echo", arguments={"message": "step 1"}),
                    ToolCall(id="c2", name="echo_echo", arguments={"message": "step 2"}),
                ],
            )
        return ModelResponse(text="Done with tools.")


class TransientFailingProvider(BaseModelProvider):
    def __init__(self, fail_count: int = 1):
        self.attempts = 0
        self.fail_count = fail_count

    async def generate_response(self, messages, tools=None, system_prompt=""):
        self.attempts += 1
        if self.attempts <= self.fail_count:
            raise ConnectionError("Transient network glitch: 503 Service Unavailable")
        return ModelResponse(text="Recovered from transient error.")


@pytest.fixture
def temp_storage():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = Storage(db_url=f"sqlite:///{db_path}")
    yield store
    try:
        os.remove(db_path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_cancel_command_records_state_and_returns_summary(temp_storage, monkeypatch):
    """
    /cancel stores cancellation state and returns response.
    """
    import backend.app.agent as agent_mod
    monkeypatch.setattr(agent_mod, "storage", temp_storage)

    runner = AgentRunner()
    req = RunRequest(
        request_id="tg:601",
        update_id=601,
        user_id=12345,
        chat_id=12345,
        message_id=1,
        text="/cancel",
    )

    resp = await runner.run(req)
    assert resp.status == "completed"
    assert "cancelled" in resp.answer.lower()
    assert await temp_storage.is_chat_cancelled(12345) is True


@pytest.mark.asyncio
async def test_cancellation_prevents_next_tool_call(temp_storage, monkeypatch):
    """
    Complete when: Cancellation prevents the next tool call.
    """
    import backend.app.agent as agent_mod
    monkeypatch.setattr(agent_mod, "storage", temp_storage)

    provider = TwoStepToolProvider()
    runner = AgentRunner(provider=provider)

    tool_execution_count = 0
    orig_execute = mcp_manager.execute_tool

    async def tracking_execute(namespaced_tool_name, arguments, **kwargs):
        nonlocal tool_execution_count
        tool_execution_count += 1
        # Set cancellation state right during first tool execution
        await temp_storage.set_chat_cancelled(12345, "User cancelled during first tool")
        return {"text": f"Executed {namespaced_tool_name}"}

    monkeypatch.setattr(mcp_manager, "execute_tool", tracking_execute)

    req = RunRequest(
        request_id="tg:602",
        update_id=602,
        user_id=12345,
        chat_id=12345,
        message_id=2,
        text="Run two tools",
    )

    resp = await runner.run(req)
    assert resp.status == "cancelled"
    assert resp.error_code == "CANCELLED"
    # Exactly one tool was executed; the second tool call was PREVENTED by cancellation!
    assert tool_execution_count == 1
    assert "cancelled before executing tool" in resp.answer.lower()


@pytest.mark.asyncio
async def test_completed_write_actions_remain_recorded_on_cancellation(temp_storage, monkeypatch):
    """
    Complete when: Completed write actions remain recorded and are never described as rolled back without evidence.
    """
    import backend.app.agent as agent_mod
    monkeypatch.setattr(agent_mod, "storage", temp_storage)

    # Simulate an executed write action recorded previously in this chat
    action = ActionRecord(
        action_id="act_transfer_999",
        idempotency_key="idemp_999",
        chat_id=12345,
        user_id=12345,
        tool_name="financial_transfer",
        arguments={"amount": 100, "to": "savings"},
        confirmation_token="tok123",
        status="executed",
        executed_at=1700000000.0,
        result="Transferred $100 successfully",
    )
    conn = temp_storage._get_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO write_actions (
                action_id, idempotency_key, chat_id, user_id, tool_name,
                arguments, confirmation_token, status, created_at, expires_at, executed_at, result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action.action_id,
                action.idempotency_key,
                action.chat_id,
                action.user_id,
                action.tool_name,
                '{"amount": 100, "to": "savings"}',
                action.confirmation_token,
                action.status,
                action.created_at,
                action.expires_at,
                action.executed_at,
                action.result,
            ),
        )
    conn.close()

    runner = AgentRunner()
    req = RunRequest(
        request_id="tg:603",
        update_id=603,
        user_id=12345,
        chat_id=12345,
        message_id=3,
        text="/cancel",
    )

    resp = await runner.run(req)
    assert resp.status == "completed"
    # Verifies completed write actions remain recorded and explicitly NOT described as rolled back
    assert "Completed write actions remain recorded and are NOT rolled back" in resp.answer
    assert "financial_transfer" in resp.answer
    assert "act_transfer_999" in resp.answer

    # Verify action in database is still 'executed'
    executed_list = await temp_storage.get_executed_actions_for_chat(12345)
    assert len(executed_list) == 1
    assert executed_list[0].status == "executed"


@pytest.mark.asyncio
async def test_transient_provider_failure_retry_within_budget(temp_storage, monkeypatch):
    """
    Retry transient provider failures only inside the budget.
    """
    import backend.app.agent as agent_mod
    monkeypatch.setattr(agent_mod, "storage", temp_storage)

    # Provider fails once then recovers
    failing_provider = TransientFailingProvider(fail_count=1)
    runner = AgentRunner(provider=failing_provider, deadline_seconds=10.0)

    req = RunRequest(
        request_id="tg:604",
        update_id=604,
        user_id=12345,
        chat_id=12345,
        message_id=4,
        text="Hello with transient glitch",
    )

    resp = await runner.run(req)
    assert resp.status == "completed"
    assert resp.answer == "Recovered from transient error."
    assert failing_provider.attempts == 2
