import json
import pytest
from backend.app.structured_logging import (
    redact_sensitive_text,
    build_structured_log,
    log_structured_event,
)


def test_redact_sensitive_text_tokens_and_media_urls():
    raw_text = (
        "Received audio from https://api.telegram.org/file/bot123456789:ABCdefGhIjklMNopQRstUVwxyz123456/voice.oga "
        "using bot token 987654321:XYZ987654321abcdefgHIJKLMNopqrstuvwxyz "
        "and header Bearer secret_bearer_token_xyz_123 "
        "with key AIzaSyD1234567890abcdefghijklmnopqrstuv"
    )
    redacted = redact_sensitive_text(raw_text)

    assert "123456789:ABC" not in redacted
    assert "987654321:XYZ" not in redacted
    assert "secret_bearer_token" not in redacted
    assert "AIzaSyD" not in redacted
    assert "voice.oga" not in redacted
    assert "[REDACTED_MEDIA_URL]" in redacted
    assert "[REDACTED_BOT_TOKEN]" in redacted
    assert "[REDACTED_TOKEN]" in redacted
    assert "[REDACTED_API_KEY]" in redacted


def test_build_structured_log_default_redaction():
    entry = build_structured_log(
        event="agent_run",
        status="completed",
        duration_seconds=1.23456,
        request_id="req-test-99",
        job_id="job-test-88",
        chat_id=12345,
        tool_names=["echo_add", "notes_read"],
        model_usage={"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
        error_code=None,
    )

    # Core required fields
    assert entry["event"] == "agent_run"
    assert entry["status"] == "completed"
    assert entry["duration_seconds"] == 1.2346
    assert entry["request_id"] == "req-test-99"
    assert entry["job_id"] == "job-test-88"
    assert entry["chat_id"] == 12345
    assert entry["tool_names"] == ["echo_add", "notes_read"]
    assert entry["model_usage"] == {
        "prompt_tokens": 120,
        "completion_tokens": 40,
        "total_tokens": 160,
    }

    # Redacted by default
    assert entry["personal_content"] == "[REDACTED_BY_DEFAULT]"
    assert entry["tool_results"] == "[REDACTED_BY_DEFAULT]"


def test_log_structured_event_outputs_json(caplog):
    import logging
    caplog.set_level(logging.INFO)

    log_structured_event(
        event="job_completed",
        status="completed",
        duration_seconds=0.75,
        request_id="req-job-001",
        job_id="job_abc123",
        chat_id=777,
        tool_names=["echo_echo"],
        model_usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
    )

    found = False
    for record in caplog.records:
        try:
            parsed = json.loads(record.message)
            if parsed.get("job_id") == "job_abc123":
                found = True
                assert parsed["status"] == "completed"
                assert parsed["tool_names"] == ["echo_echo"]
                assert parsed["model_usage"]["total_tokens"] == 60
                break
        except Exception:
            continue

    assert found, "Structured log JSON entry must be emitted"
