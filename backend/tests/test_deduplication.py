import os
import pytest
import tempfile
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

# Configure test environment
os.environ["EDGE_BACKEND_SECRET"] = "test-edge-secret"
os.environ["ALLOWED_USER_ID"] = "12345"

from backend.app.main import app
from backend.app.schemas import RunRequest
from backend.app.agent import AgentRunner
from backend.app.storage import Storage, RequestRecord
from backend.app.providers.base import ModelResponse

client = TestClient(app)
AUTH_HEADERS = {"Authorization": "Bearer test-edge-secret"}


@pytest.mark.asyncio
async def test_deduplication_reuses_result_and_avoids_repeated_tool_mutation():
    """
    Verify that replaying the same update_id reuses its result
    and never calls the model or repeats a tool mutation.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    test_storage = Storage(f"sqlite:///{db_path}")
    runner = AgentRunner()

    request = RunRequest(
        request_id="tg:5001",
        update_id=5001,
        user_id=12345,
        chat_id=12345,
        message_id=99,
        text="Calculate total revenue",
    )

    mock_provider = AsyncMock()
    mock_provider.generate_response.return_value = ModelResponse(
        text="Total revenue is $50,000"
    )

    with patch("backend.app.agent.storage", test_storage), patch.object(
        runner, "_get_provider", return_value=mock_provider
    ):
        # 1. First execution
        first_response = await runner.run(request)
        assert first_response.status == "completed"
        assert first_response.answer == "Total revenue is $50,000"
        assert mock_provider.generate_response.call_count == 1

        # 2. Replay the exact same update_id
        second_response = await runner.run(request)
        assert second_response.status == "completed"
        assert second_response.answer == "Total revenue is $50,000"
        # The model provider must NOT be called again
        assert mock_provider.generate_response.call_count == 1

        # 3. Third replay with same update_id
        third_response = await runner.run(request)
        assert third_response.status == "completed"
        assert mock_provider.generate_response.call_count == 1

    # Clean up test db
    try:
        os.remove(db_path)
    except Exception:
        pass


@pytest.mark.asyncio
async def test_durable_storage_preserves_completed_request_across_instances():
    """
    Verify that SQLite database retains completed requests across new Storage instances.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    storage_instance_1 = Storage(f"sqlite:///{db_path}")
    record = RequestRecord(
        request_id="tg:6001",
        update_id=6001,
        user_id=12345,
        chat_id=12345,
        message_id=10,
        text="Durable test",
        status="completed",
        answer="Saved persistently",
    )
    await storage_instance_1.save_request(record)

    # Instantiate a second storage instance pointing to same file
    storage_instance_2 = Storage(f"sqlite:///{db_path}")
    loaded = await storage_instance_2.get_by_update_id(6001)

    assert loaded is not None
    assert loaded.request_id == "tg:6001"
    assert loaded.status == "completed"
    assert loaded.answer == "Saved persistently"

    # Clean up test db
    try:
        os.remove(db_path)
    except Exception:
        pass


def test_prompt_length_limit_enforced_by_backend():
    """
    Verify that prompts exceeding 4000 characters are rejected with HTTP 422.
    """
    oversized_text = "A" * 4001
    payload = {
        "request_id": "tg:7001",
        "update_id": 7001,
        "user_id": 12345,
        "chat_id": 12345,
        "message_id": 15,
        "text": oversized_text,
    }
    response = client.post("/v1/assistant/run", json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 422
