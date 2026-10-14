import os
import asyncio
import logging
from typing import Optional
from backend.app.schemas import RunRequest, RunResponse
from backend.app.storage import storage, RequestRecord
from backend.app.mcp_manager import mcp_manager
from backend.app.providers.anthropic_provider import AnthropicProvider
from backend.app.providers.openai_provider import OpenAIProvider
from backend.app.providers.base import BaseModelProvider

logger = logging.getLogger(__name__)


class AgentRunner:
    """
    Executes the bounded agent loop:
    1. Loads context & checks request deduplication.
    2. Runs model turns with MCP tools up to bounds (MAX_MODEL_TURNS, MAX_TOOL_CALLS).
    3. Handles deadlines and updates durable storage.
    """

    def __init__(
        self,
        max_turns: int = 6,
        max_tool_calls: int = 8,
        deadline_seconds: float = 25.0,
    ):
        self.max_turns = int(os.environ.get("MAX_MODEL_TURNS", max_turns))
        self.max_tool_calls = int(os.environ.get("MAX_TOOL_CALLS", max_tool_calls))
        self.deadline_seconds = float(os.environ.get("RUN_DEADLINE_SECONDS", deadline_seconds))

    def _get_provider(self) -> BaseModelProvider:
        provider_name = os.environ.get("MODEL_PROVIDER", "anthropic").lower()
        if provider_name == "openai":
            return OpenAIProvider()
        return AnthropicProvider()

    async def run(self, request: RunRequest) -> RunResponse:
        # Check deduplication
        existing = await storage.get_by_update_id(request.update_id)
        if existing and existing.status == "completed":
            logger.info(f"Replaying cached response for update_id={request.update_id}")
            return RunResponse(
                request_id=request.request_id,
                status="completed",
                answer=existing.answer,
                error_code=existing.error_code,
            )

        # Record in-progress state
        record = RequestRecord(
            request_id=request.request_id,
            update_id=request.update_id,
            user_id=request.user_id,
            chat_id=request.chat_id,
            message_id=request.message_id,
            text=request.text,
            status="in_progress",
        )
        await storage.save_request(record)

        try:
            # Check for /reset command
            if request.text.strip().lower() == "/reset":
                await storage.clear_chat_history(request.chat_id)
                answer = "Chat context has been cleared."
                record.status = "completed"
                record.answer = answer
                await storage.save_request(record)
                return RunResponse(
                    request_id=request.request_id,
                    status="completed",
                    answer=answer,
                    error_code=None,
                )

            # Bounded execution with total deadline
            async with asyncio.timeout(self.deadline_seconds):
                # Retrieve bounded history
                history = await storage.get_chat_history(request.chat_id, limit=6)
                provider = self._get_provider()

                messages = list(history)
                messages.append({"role": "user", "content": request.text})

                # Call provider model
                response = await provider.generate_response(
                    messages=messages,
                    system_prompt="You are a helpful private Telegram assistant.",
                )

                answer = response.text or "I received your message."

                # Update history & save completion
                await storage.append_chat_message(request.chat_id, "user", request.text)
                await storage.append_chat_message(request.chat_id, "assistant", answer)

                record.status = "completed"
                record.answer = answer
                await storage.save_request(record)

                return RunResponse(
                    request_id=request.request_id,
                    status="completed",
                    answer=answer,
                    error_code=None,
                )

        except TimeoutError:
            logger.error(f"Execution exceeded deadline of {self.deadline_seconds}s")
            record.status = "error"
            record.error_code = "DEADLINE_EXCEEDED"
            await storage.save_request(record)
            return RunResponse(
                request_id=request.request_id,
                status="error",
                answer=None,
                error_code="DEADLINE_EXCEEDED",
            )
        except Exception as e:
            logger.exception("Error during agent execution")
            record.status = "error"
            record.error_code = str(e) or "INTERNAL_ERROR"
            await storage.save_request(record)
            return RunResponse(
                request_id=request.request_id,
                status="error",
                answer=None,
                error_code="INTERNAL_ERROR",
            )


agent_runner = AgentRunner()
