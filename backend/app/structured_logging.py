import os
import re
import json
import time
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger("structured_logger")

# Regex patterns for redaction
BOT_TOKEN_PATTERN = re.compile(r"(?:bot)?\b\d{8,12}:[A-Za-z0-9_-]{30,}\b", re.IGNORECASE)
BEARER_TOKEN_PATTERN = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/-]+", re.IGNORECASE)
GEMINI_API_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z-_]{35}")
MEDIA_URL_PATTERN = re.compile(
    r"https?://api\.telegram\.org/file/bot[^\s\"'>]+|https?://[^\s\"'>]+\.(?:oga|ogg|mp3|wav|m4a|mp4)[^\s\"'>]*",
    re.IGNORECASE,
)


def redact_sensitive_text(text: str) -> str:
    """
    Redact tokens and media URLs from raw string messages.
    """
    if not text or not isinstance(text, str):
        return text
    redacted = BOT_TOKEN_PATTERN.sub("[REDACTED_BOT_TOKEN]", text)
    redacted = BEARER_TOKEN_PATTERN.sub(r"\1[REDACTED_TOKEN]", redacted)
    redacted = GEMINI_API_KEY_PATTERN.sub("[REDACTED_API_KEY]", redacted)
    redacted = MEDIA_URL_PATTERN.sub("[REDACTED_MEDIA_URL]", redacted)
    return redacted


class RedactingLogFilter(logging.Filter):
    """
    Logging filter that automatically redacts tokens, URLs, and sensitive keys from all standard logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_sensitive_text(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_sensitive_text(v) if isinstance(v, str) else v
                    for v in record.args
                )
        return True


def build_structured_log(
    event: str,
    status: str,
    duration_seconds: float,
    request_id: Optional[str] = None,
    job_id: Optional[str] = None,
    chat_id: Optional[int] = None,
    tool_names: Optional[List[str]] = None,
    model_usage: Optional[Dict[str, int]] = None,
    error_code: Optional[str] = None,
    redact_personal_content: bool = True,
    redact_tool_results: bool = True,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Constructs a structured JSON log entry enforcing default redaction of
    personal content, tool results, tokens, and media URLs.
    """
    log_data: Dict[str, Any] = {
        "timestamp": time.time(),
        "event": event,
        "status": status,
        "duration_seconds": round(duration_seconds, 4),
        "request_id": request_id,
        "job_id": job_id,
        "chat_id": chat_id,
        "tool_names": tool_names or [],
        "model_usage": model_usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "error_code": error_code,
    }

    if redact_personal_content:
        log_data["personal_content"] = "[REDACTED_BY_DEFAULT]"
    if redact_tool_results:
        log_data["tool_results"] = "[REDACTED_BY_DEFAULT]"

    if extra:
        for k, v in extra.items():
            if isinstance(v, str):
                log_data[k] = redact_sensitive_text(v)
            else:
                log_data[k] = v

    return log_data


def log_structured_event(
    event: str,
    status: str,
    duration_seconds: float,
    request_id: Optional[str] = None,
    job_id: Optional[str] = None,
    chat_id: Optional[int] = None,
    tool_names: Optional[List[str]] = None,
    model_usage: Optional[Dict[str, int]] = None,
    error_code: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Format and emit a structured log JSON entry.
    """
    data = build_structured_log(
        event=event,
        status=status,
        duration_seconds=duration_seconds,
        request_id=request_id,
        job_id=job_id,
        chat_id=chat_id,
        tool_names=tool_names,
        model_usage=model_usage,
        error_code=error_code,
        redact_personal_content=True,
        redact_tool_results=True,
        extra=extra,
    )
    logger.info(json.dumps(data))
    return data
