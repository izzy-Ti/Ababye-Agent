import os
import tempfile
import asyncio
import logging
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from backend.app.schemas import RunRequest, VoiceMetadata
from backend.app.storage import Storage
from backend.app.agent import AgentRunner
from backend.app.audio import AudioService, VoiceProcessingError, audio_service
from backend.app.providers.base import BaseModelProvider, ModelResponse


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
async def test_voice_note_produces_transcript_based_answer(test_storage):
    """
    Complete when: A voice note produces a transcript-based answer.
    Verifies that voice is transcribed and passed through the same text pipeline.
    """
    mock_audio_bytes = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00FAKE_OGG_AUDIO_BYTES"

    # Mock transcriber function
    async def mock_transcribe(data: bytes, mime: str) -> str:
        return "What is the capital of France?"

    service = AudioService(
        bot_token="fake_bot_token_123",
        transcriber_fn=mock_transcribe,
    )

    # Mock Telegram file download
    with patch.object(service, "fetch_audio_bytes", return_value=mock_audio_bytes):
        with patch("backend.app.agent.audio_service", service):
            class EchoModel(BaseModelProvider):
                async def generate_response(self, messages, tools=None, system_prompt=None):
                    user_prompt = messages[-1]["content"]
                    return ModelResponse(
                        text=f"The answer to '{user_prompt}' is Paris.",
                        stop_reason="stop",
                    )

            runner = AgentRunner(provider=EchoModel())

            req = RunRequest(
                request_id="tg:voice-1",
                update_id=3001,
                user_id=12345,
                chat_id=9001,
                message_id=1,
                voice=VoiceMetadata(
                    file_id="voice_file_abc123",
                    mime_type="audio/ogg",
                    file_size=1024,
                    duration=5,
                ),
            )

            response = await runner.run(req)

            assert response.status == "completed"
            assert response.error_code is None
            assert "The answer to 'What is the capital of France?' is Paris." in response.answer

            # Verify history stored the transcript
            history = await test_storage.get_chat_history(9001)
            assert len(history) == 2
            assert history[0]["role"] == "user"
            assert history[0]["content"] == "What is the capital of France?"


@pytest.mark.asyncio
async def test_oversized_voice_file_returns_controlled_error(test_storage):
    """
    Complete when: Oversized download returns controlled error VOICE_FILE_TOO_LARGE.
    """
    service = AudioService(bot_token="test_bot_token")

    with patch("backend.app.agent.audio_service", service):
        runner = AgentRunner()

        # Reported size exceeds 20MB
        req = RunRequest(
            request_id="tg:voice-oversize",
            update_id=3002,
            user_id=12345,
            chat_id=9002,
            message_id=2,
            voice=VoiceMetadata(
                file_id="huge_file_id",
                mime_type="audio/ogg",
                file_size=25 * 1024 * 1024,  # 25 MB
            ),
        )

        response = await runner.run(req)
        assert response.status == "error"
        assert response.error_code == "VOICE_FILE_TOO_LARGE"
        assert "maximum size limit" in response.answer.lower()


@pytest.mark.asyncio
async def test_unsupported_audio_type_returns_controlled_error(test_storage):
    """
    Complete when: Unsupported audio type returns controlled error VOICE_UNSUPPORTED_TYPE.
    """
    service = AudioService(bot_token="test_bot_token")

    with patch("backend.app.agent.audio_service", service):
        runner = AgentRunner()

        req = RunRequest(
            request_id="tg:voice-bad-type",
            update_id=3003,
            user_id=12345,
            chat_id=9003,
            message_id=3,
            voice=VoiceMetadata(
                file_id="unsupported_file_id",
                mime_type="audio/amr",  # AMR not supported
            ),
        )

        response = await runner.run(req)
        assert response.status == "error"
        assert response.error_code == "VOICE_UNSUPPORTED_TYPE"
        assert "unsupported voice audio format" in response.answer.lower()


@pytest.mark.asyncio
async def test_failed_download_returns_controlled_error(test_storage):
    """
    Complete when: Failed download returns controlled error VOICE_DOWNLOAD_FAILED.
    """
    service = AudioService(bot_token="test_bot_token")

    # Simulate Telegram API error (e.g. 404 file not found)
    async def mock_handler(request: httpx.Request):
        return httpx.Response(404, json={"ok": False, "description": "Bad Request: file not found"})

    transport = httpx.MockTransport(mock_handler)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        with patch("backend.app.agent.audio_service", service):
            runner = AgentRunner()

            req = RunRequest(
                request_id="tg:voice-failed-dl",
                update_id=3004,
                user_id=12345,
                chat_id=9004,
                message_id=4,
                voice=VoiceMetadata(
                    file_id="non_existent_file_id",
                    mime_type="audio/ogg",
                ),
            )

            response = await runner.run(req)
            assert response.status == "error"
            assert response.error_code == "VOICE_DOWNLOAD_FAILED"


@pytest.mark.asyncio
async def test_tokens_and_download_urls_kept_out_of_logs(caplog):
    """
    Keep download URLs and tokens out of logs.
    """
    secret_token = "SECRET_BOT_TOKEN_999888"
    secret_url = f"https://api.telegram.org/file/bot{secret_token}/voice/test.oga"

    service = AudioService(bot_token=secret_token)

    async def mock_handler(request: httpx.Request):
        if "getFile" in str(request.url):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_id": "v1", "file_path": "voice/test.oga", "file_size": 50}},
            )
        return httpx.Response(200, content=b"audio_bytes_data")

    transport = httpx.MockTransport(mock_handler)

    with caplog.at_level(logging.INFO):
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            await service.fetch_audio_bytes(VoiceMetadata(file_id="v1", mime_type="audio/ogg"))

    # Assert secret token is NOT in any log record
    log_text = caplog.text
    assert secret_token not in log_text
    assert "SECRET_BOT_TOKEN" not in log_text
    # Assert URL is redacted
    assert "voice/test.oga" not in log_text
    assert "[TOKEN REDACTED]" in log_text or "[URL REDACTED]" in log_text
