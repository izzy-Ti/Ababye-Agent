import os
import asyncio
import logging
from typing import Optional, Dict, Any, List
from backend.app.schemas import RunRequest, RunResponse
from backend.app.storage import storage, RequestRecord
from backend.app.mcp_manager import mcp_manager
from backend.app.providers.gemini_provider import GeminiProvider
from backend.app.providers.base import BaseModelProvider

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_POLICY = (
    "You are a private Telegram assistant. "
    "Execute tasks accurately using available tools. "
    "Provide concise, direct, Telegram-safe answers."
)


class AgentRunner:
    """
    Executes the bounded agent loop:
    1. Loads context & checks request deduplication.
    2. Serializes execution per chat (1 active run per chat).
    3. Runs model turns with MCP tools up to bounds (MAX_MODEL_TURNS, MAX_TOOL_CALLS).
    4. Bounds context size and per-tool output size.
    5. Stops on final answer, deadline, cancellation, or limits.
    6. Returns a useful partial-status message on limit exhaustion without claiming success.
    """

    def __init__(
        self,
        max_turns: int = 6,
        max_tool_calls: int = 8,
        deadline_seconds: float = 25.0,
        provider: Optional[BaseModelProvider] = None,
    ):
        self.max_turns = int(os.environ.get("MAX_MODEL_TURNS", max_turns))
        self.max_tool_calls = int(os.environ.get("MAX_TOOL_CALLS", max_tool_calls))
        self.deadline_seconds = float(os.environ.get("RUN_DEADLINE_SECONDS", deadline_seconds))
        self._provider = provider
        self._chat_locks: Dict[int, asyncio.Lock] = {}

    def _get_chat_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    def _get_provider(self) -> BaseModelProvider:
        if self._provider is not None:
            return self._provider
        return GeminiProvider()

    def _bound_context_messages(
        self,
        history: List[Dict[str, Any]],
        user_prompt: str,
        max_chars: int = 12000,
    ) -> List[Dict[str, Any]]:
        """
        Assemble history and user prompt, ensuring combined context does not exceed character bounds.
        Drops oldest history turns if total size exceeds max_chars.
        """
        user_msg = {"role": "user", "content": user_prompt}
        total_len = len(user_prompt)

        bounded_history: List[Dict[str, Any]] = []
        for msg in reversed(history):
            content_len = len(str(msg.get("content", "")))
            if total_len + content_len > max_chars:
                break
            bounded_history.insert(0, msg)
            total_len += content_len

        bounded_history.append(user_msg)
        return bounded_history

    async def run(self, request: RunRequest) -> RunResponse:
        # 1. Authoritative deduplication check
        existing = await storage.get_by_update_id(request.update_id)
        if existing:
            if existing.status == "completed":
                logger.info(f"Replaying cached response for update_id={request.update_id}")
                return RunResponse(
                    request_id=request.request_id,
                    status="completed",
                    answer=existing.answer,
                    error_code=existing.error_code,
                )
            elif existing.status == "in_progress":
                logger.info(f"Duplicate request in progress for update_id={request.update_id}, waiting...")
                for _ in range(25):  # up to 5 seconds
                    await asyncio.sleep(0.2)
                    check = await storage.get_by_update_id(request.update_id)
                    if check and check.status == "completed":
                        return RunResponse(
                            request_id=request.request_id,
                            status="completed",
                            answer=check.answer,
                            error_code=check.error_code,
                        )
                return RunResponse(
                    request_id=request.request_id,
                    status="in_progress",
                    answer=None,
                    error_code=None,
                )

        # 2. Serialize execution per chat (1 active run per chat)
        chat_lock = self._get_chat_lock(request.chat_id)
        async with chat_lock:
            # Re-check deduplication in case a concurrent request finished while waiting for lock
            existing = await storage.get_by_update_id(request.update_id)
            if existing and existing.status == "completed":
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
                # Handle /reset command
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

                # Ensure MCP tools are ready
                if not mcp_manager._initialized:
                    await mcp_manager.initialize()

                tools = await mcp_manager.get_tools()
                tools_dict = [t.to_dict() for t in tools] if tools else None

                # Bounded execution with total deadline
                start_time = asyncio.get_running_loop().time()
                async with asyncio.timeout(self.deadline_seconds):
                    # Load bounded history
                    history = await storage.get_chat_history(request.chat_id, limit=6)
                    messages = self._bound_context_messages(history, request.text)

                    provider = self._get_provider()
                    turns_count = 0
                    tool_calls_count = 0
                    last_model_text: Optional[str] = None
                    limit_exhausted = False
                    exhaustion_reason = ""

                    while True:
                        # Check turn limit
                        if turns_count >= self.max_turns:
                            limit_exhausted = True
                            exhaustion_reason = f"Reached maximum allowed model turns ({self.max_turns})."
                            break

                        turns_count += 1
                        elapsed = asyncio.get_running_loop().time() - start_time
                        remaining_deadline = max(0.5, self.deadline_seconds - elapsed)

                        # Request next turn from model provider
                        response = await provider.generate_response(
                            messages=messages,
                            tools=tools_dict,
                            system_prompt=DEFAULT_SYSTEM_POLICY,
                        )

                        if response.text:
                            last_model_text = response.text

                        # Stop condition: Final answer reached (no tool calls requested)
                        if not response.tool_calls:
                            final_answer = response.text or "I received your message."

                            # Update persistent chat history with the user input and final answer
                            await storage.append_chat_message(request.chat_id, "user", request.text)
                            await storage.append_chat_message(request.chat_id, "assistant", final_answer)

                            record.status = "completed"
                            record.answer = final_answer
                            await storage.save_request(record)

                            return RunResponse(
                                request_id=request.request_id,
                                status="completed",
                                answer=final_answer,
                                error_code=None,
                            )

                        # Check tool call limit before executing
                        if tool_calls_count + len(response.tool_calls) > self.max_tool_calls:
                            limit_exhausted = True
                            exhaustion_reason = (
                                f"Reached maximum tool call budget ({self.max_tool_calls})."
                            )
                            break

                        # Append assistant turn to messages
                        messages.append({
                            "role": "assistant",
                            "content": response.text,
                            "tool_calls": [tc.model_dump() for tc in response.tool_calls],
                        })

                        # Execute approved tool calls
                        for tc in response.tool_calls:
                            tool_calls_count += 1
                            elapsed = asyncio.get_running_loop().time() - start_time
                            remaining = max(0.5, self.deadline_seconds - elapsed)
                            # Keep per-tool timeout shorter than remaining total deadline
                            per_tool_timeout = min(10.0, max(1.0, remaining - 1.0))

                            try:
                                tool_result = await mcp_manager.execute_tool(
                                    namespaced_tool_name=tc.name,
                                    arguments=tc.arguments,
                                    timeout_seconds=per_tool_timeout,
                                )
                                raw_text = tool_result.get("text", "")
                                # Bound each tool result length (4000 chars) to prevent context blowout
                                if len(raw_text) > 4000:
                                    result_text = raw_text[:4000] + "\n...[truncated output exceeds 4000 chars]"
                                else:
                                    result_text = raw_text
                            except Exception as tool_err:
                                logger.warning(f"Tool execution failed for {tc.name}: {tool_err}")
                                result_text = f"Error executing tool {tc.name}: {str(tool_err)}"

                            # Append tool result to messages for the next model turn
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "name": tc.name,
                                "content": result_text,
                            })

                    # If loop broke due to limit exhaustion:
                    if limit_exhausted:
                        logger.warning(f"Agent loop limit exhausted: {exhaustion_reason}")
                        partial_status_msg = (
                            f"Execution stopped: {exhaustion_reason} "
                            f"Completed {turns_count} turns and {tool_calls_count} tool calls. "
                            f"Partial progress: {last_model_text or 'Stopped before completing all requested actions.'}"
                        )
                        record.status = "error"
                        record.error_code = "LIMIT_EXHAUSTED"
                        record.answer = partial_status_msg
                        await storage.save_request(record)

                        return RunResponse(
                            request_id=request.request_id,
                            status="error",
                            answer=partial_status_msg,
                            error_code="LIMIT_EXHAUSTED",
                        )

            except TimeoutError:
                logger.error(f"Execution exceeded deadline of {self.deadline_seconds}s")
                timeout_msg = f"Execution exceeded deadline of {self.deadline_seconds}s."
                record.status = "error"
                record.error_code = "DEADLINE_EXCEEDED"
                record.answer = timeout_msg
                await storage.save_request(record)
                return RunResponse(
                    request_id=request.request_id,
                    status="error",
                    answer=timeout_msg,
                    error_code="DEADLINE_EXCEEDED",
                )
            except Exception as e:
                logger.exception("Error during agent execution")
                record.status = "error"
                record.error_code = "INTERNAL_ERROR"
                record.answer = str(e) or "An internal error occurred."
                await storage.save_request(record)
                return RunResponse(
                    request_id=request.request_id,
                    status="error",
                    answer=record.answer,
                    error_code="INTERNAL_ERROR",
                )


agent_runner = AgentRunner()
