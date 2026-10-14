from typing import Optional, Literal
from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    request_id: str = Field(..., description="Unique request identifier, e.g. tg:12345")
    update_id: int = Field(..., description="Telegram update ID for deduplication")
    user_id: int = Field(..., description="Telegram user ID of the sender")
    chat_id: int = Field(..., description="Telegram chat ID")
    message_id: int = Field(..., description="Telegram message ID")
    text: str = Field(..., min_length=1, max_length=4000, description="Normalized user text prompt")


class RunResponse(BaseModel):
    request_id: str = Field(..., description="Request identifier matching the input")
    status: Literal["completed", "error", "in_progress"] = Field(
        ..., description="Execution status of the assistant run"
    )
    answer: Optional[str] = Field(None, description="Final assistant reply text")
    error_code: Optional[str] = Field(None, description="Machine-readable error code if failed")


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
