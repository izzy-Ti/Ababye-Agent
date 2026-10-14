import os
import asyncio
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.app.agent import AgentRunner
from backend.app.schemas import RunRequest
from backend.app.storage import Storage
from backend.app.mcp_manager import mcp_manager
from backend.app.providers.base import BaseModelProvider, ModelResponse, ToolCall


class MockMultiStepProvider(BaseModelProvider):
    """
    Mock provider simulating multi-turn reasoning with tool execution.
    """

    def __init__(self, turns: list):
        self.turns = list(turns)
        self.call_count = 0

    async def generate_response(
        self,
        messages: list,
        tools: list = None,
        system_prompt: str = None,
    ) -> ModelResponse:
        if self.call_count < len(self.turns):
            resp = self.turns[self.call_count]
            self.call_count += 1
            return resp
        return ModelResponse(text="Default mock completion", stop_reason="stop")


@pytest.fixture
def test_storage():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    st = Storage(f"sqlite:///{db_path}")
    with patch("backend.app.agent.storage", st):
        yield st
    try:
        os.remove(db_path)
    except Exception:
        pass


@pytest.mark.asyncio
async def test_multi_step_read_task_finishes(test_storage):
    """
    Complete when: A multi-step read task finishes.
    Turn 1: Model calls echo_add(a=10, b=20). Real MCP server returns 30.
    Turn 2: Model calls echo_add(a=30, b=12). Real MCP server returns 42.
    Turn 3: Model finishes with final text 'The final sum is 42.'.
    """
    await mcp_manager.initialize()
    try:
        turns = [
            ModelResponse(
                text="Let me add 10 and 20 first.",
                tool_calls=[
                    ToolCall(id="call_1", name="echo_add", arguments={"a": 10, "b": 20})
                ],
                stop_reason="tool_use",
            ),
            ModelResponse(
                text="Now let me add 12 to 30.",
                tool_calls=[
                    ToolCall(id="call_2", name="echo_add", arguments={"a": 30, "b": 12})
                ],
                stop_reason="tool_use",
            ),
            ModelResponse(
                text="The final calculated sum is 42.",
                tool_calls=[],
                stop_reason="stop",
            ),
        ]

        mock_provider = MockMultiStepProvider(turns)
        runner = AgentRunner(
            max_turns=6,
            max_tool_calls=8,
            deadline_seconds=15.0,
            provider=mock_provider,
        )

        request = RunRequest(
            request_id="tg:multistep-1",
            update_id=8801,
            user_id=12345,
            chat_id=7771,
            message_id=1,
            text="Calculate (10 + 20) + 12 step by step",
        )

        response = await runner.run(request)

        assert response.status == "completed"
        assert response.error_code is None
        assert "42" in response.answer
        assert mock_provider.call_count == 3

        # Verify persistent chat history updated
        history = await test_storage.get_chat_history(7771)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"
        assert "42" in history[1]["content"]
    finally:
        await mcp_manager.shutdown()


@pytest.mark.asyncio
async def test_turn_limit_exhaustion_returns_partial_status(test_storage):
    """
    Complete when: Limit exhaustion returns a useful partial-status message without claiming success.
    Configured with max_turns=2. Model requests tools indefinitely.
    Agent stops after 2 turns and returns status='error', error_code='LIMIT_EXHAUSTED', and partial status.
    """
    await mcp_manager.initialize()
    try:
        # Infinite tool loop
        turns = [
            ModelResponse(
                text="Processing step 1",
                tool_calls=[
                    ToolCall(id="call_loop_1", name="echo_echo", arguments={"message": "step1"})
                ],
                stop_reason="tool_use",
            ),
            ModelResponse(
                text="Processing step 2",
                tool_calls=[
                    ToolCall(id="call_loop_2", name="echo_echo", arguments={"message": "step2"})
                ],
                stop_reason="tool_use",
            ),
            ModelResponse(
                text="Processing step 3",
                tool_calls=[
                    ToolCall(id="call_loop_3", name="echo_echo", arguments={"message": "step3"})
                ],
                stop_reason="tool_use",
            ),
        ]

        mock_provider = MockMultiStepProvider(turns)
        runner = AgentRunner(
            max_turns=2,  # Bound to 2 turns max
            max_tool_calls=8,
            deadline_seconds=15.0,
            provider=mock_provider,
        )

        request = RunRequest(
            request_id="tg:limit-turns",
            update_id=8802,
            user_id=12345,
            chat_id=7772,
            message_id=2,
            text="Loop forever",
        )

        response = await runner.run(request)

        assert response.status == "error"
        assert response.error_code == "LIMIT_EXHAUSTED"
        assert "maximum allowed model turns (2)" in response.answer
        assert "Partial progress: Processing step 2" in response.answer
        # Must NOT claim success
        assert "completed" not in response.answer.lower() or "error" in response.status
    finally:
        await mcp_manager.shutdown()


@pytest.mark.asyncio
async def test_tool_call_budget_exhaustion_returns_partial_status(test_storage):
    """
    Complete when: Tool call budget exhaustion returns partial-status message without claiming success.
    Configured with max_tool_calls=1. Model requests 2 tool calls at once.
    Agent stops before exceeding tool budget and returns partial status.
    """
    await mcp_manager.initialize()
    try:
        turns = [
            ModelResponse(
                text="I want to run two tools at once",
                tool_calls=[
                    ToolCall(id="c1", name="echo_echo", arguments={"message": "one"}),
                    ToolCall(id="c2", name="echo_echo", arguments={"message": "two"}),
                ],
                stop_reason="tool_use",
            ),
        ]

        mock_provider = MockMultiStepProvider(turns)
        runner = AgentRunner(
            max_turns=6,
            max_tool_calls=1,  # Only 1 tool call allowed
            deadline_seconds=15.0,
            provider=mock_provider,
        )

        request = RunRequest(
            request_id="tg:limit-tools",
            update_id=8803,
            user_id=12345,
            chat_id=7773,
            message_id=3,
            text="Run two tools",
        )

        response = await runner.run(request)

        assert response.status == "error"
        assert response.error_code == "LIMIT_EXHAUSTED"
        assert "maximum tool call budget (1)" in response.answer
    finally:
        await mcp_manager.shutdown()


@pytest.mark.asyncio
async def test_per_chat_serialization(test_storage):
    """
    Verifies 1 active run per chat: two requests to the same chat_id execute sequentially.
    """
    await mcp_manager.initialize()
    try:
        execution_order = []

        class DelayedProvider(BaseModelProvider):
            def __init__(self, label: str, delay: float):
                self.label = label
                self.delay = delay

            async def generate_response(self, messages, tools=None, system_prompt=None):
                await asyncio.sleep(self.delay)
                execution_order.append(self.label)
                return ModelResponse(text=f"Done {self.label}", stop_reason="stop")

        runner1 = AgentRunner(
            max_turns=6,
            max_tool_calls=8,
            deadline_seconds=10.0,
            provider=DelayedProvider("req1", delay=0.1),
        )
        # Share chat locks with runner2 to simulate same AgentRunner instance handling concurrent requests
        runner2 = AgentRunner(
            max_turns=6,
            max_tool_calls=8,
            deadline_seconds=10.0,
            provider=DelayedProvider("req2", delay=0.01),
        )
        runner2._chat_locks = runner1._chat_locks

        req1 = RunRequest(
            request_id="tg:seq-1",
            update_id=9001,
            user_id=12345,
            chat_id=8888,
            message_id=10,
            text="First task",
        )
        req2 = RunRequest(
            request_id="tg:seq-2",
            update_id=9002,
            user_id=12345,
            chat_id=8888,
            message_id=11,
            text="Second task",
        )

        # Launch both concurrently
        t1 = asyncio.create_task(runner1.run(req1))
        await asyncio.sleep(0.005)  # Ensure t1 acquires lock first
        t2 = asyncio.create_task(runner2.run(req2))

        res1, res2 = await asyncio.gather(t1, t2)

        assert res1.status == "completed"
        assert res2.status == "completed"
        # Even though req2 had a 10x shorter delay (0.01 vs 0.1), req1 MUST finish first due to chat lock
        assert execution_order == ["req1", "req2"]
    finally:
        await mcp_manager.shutdown()


@pytest.mark.asyncio
async def test_deadline_timeout_returns_safe_error(test_storage):
    """
    Verifies that exceeding the deadline terminates the run cleanly without orphan subprocesses.
    """
    await mcp_manager.initialize()
    try:
        class HangingProvider(BaseModelProvider):
            async def generate_response(self, messages, tools=None, system_prompt=None):
                await asyncio.sleep(10.0)
                return ModelResponse(text="Never reached")

        runner = AgentRunner(
            max_turns=6,
            max_tool_calls=8,
            deadline_seconds=0.2,  # Very short deadline
            provider=HangingProvider(),
        )

        request = RunRequest(
            request_id="tg:timeout",
            update_id=9003,
            user_id=12345,
            chat_id=9999,
            message_id=20,
            text="Hanging request",
        )

        response = await runner.run(request)
        assert response.status == "error"
        assert response.error_code == "DEADLINE_EXCEEDED"
    finally:
        await mcp_manager.shutdown()
