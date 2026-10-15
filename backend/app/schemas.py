from typing import Optional, Literal
from pydantic import BaseModel, Field, model_validator


class VoiceMetadata(BaseModel):
    file_id: str = Field(..., description="Telegram voice file_id")
    mime_type: Optional[str] = Field("audio/ogg", description="MIME type, e.g. audio/ogg")
    file_size: Optional[int] = Field(None, description="Audio file size in bytes")
    duration: Optional[int] = Field(None, description="Audio duration in seconds")


class RunRequest(BaseModel):
    request_id: str = Field(..., description="Unique request identifier, e.g. tg:12345")
    update_id: int = Field(..., description="Telegram update ID for deduplication")
    user_id: int = Field(..., description="Telegram user ID of the sender")
    chat_id: int = Field(..., description="Telegram chat ID")
    message_id: int = Field(..., description="Telegram message ID")
    text: str = Field("", max_length=4000, description="Normalized user text prompt or transcript")
    voice: Optional[VoiceMetadata] = Field(None, description="Optional voice note metadata")

    @model_validator(mode="after")
    def validate_content(self) -> "RunRequest":
        if not self.text.strip() and not self.voice:
            raise ValueError("Either 'text' or 'voice' must be provided.")
        return self


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
