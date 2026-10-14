import os
import pytest
from fastapi.testclient import TestClient

# Set test environment variables before importing app
os.environ["EDGE_BACKEND_SECRET"] = "test-edge-secret"
os.environ["ALLOWED_USER_ID"] = "12345"

from backend.app.main import app

client = TestClient(app)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


def test_missing_auth_returns_401():
    payload = {
        "request_id": "tg:1001",
        "update_id": 1001,
        "user_id": 12345,
        "chat_id": 12345,
        "message_id": 1,
        "text": "Hello assistant",
    }
    response = client.post("/v1/assistant/run", json=payload)
    assert response.status_code == 401


def test_invalid_bearer_token_returns_401():
    payload = {
        "request_id": "tg:1001",
        "update_id": 1001,
        "user_id": 12345,
        "chat_id": 12345,
        "message_id": 1,
        "text": "Hello assistant",
    }
    headers = {"Authorization": "Bearer wrong-secret"}
    response = client.post("/v1/assistant/run", json=payload, headers=headers)
    assert response.status_code == 401


def test_disallowed_user_returns_403():
    payload = {
        "request_id": "tg:1002",
        "update_id": 1002,
        "user_id": 99999,  # Unauthorized user
        "chat_id": 99999,
        "message_id": 1,
        "text": "Hello unauthorized",
    }
    headers = {"Authorization": "Bearer test-edge-secret"}
    response = client.post("/v1/assistant/run", json=payload, headers=headers)
    assert response.status_code == 403


def test_invalid_payload_returns_422():
    payload = {
        "request_id": "tg:1003",
        # Missing required fields like update_id, user_id, chat_id, text
    }
    headers = {"Authorization": "Bearer test-edge-secret"}
    response = client.post("/v1/assistant/run", json=payload, headers=headers)
    assert response.status_code == 422


def test_valid_request_returns_200():
    payload = {
        "request_id": "tg:1004",
        "update_id": 1004,
        "user_id": 12345,
        "chat_id": 12345,
        "message_id": 1,
        "text": "Summarize my notes",
    }
    headers = {"Authorization": "Bearer test-edge-secret"}
    response = client.post("/v1/assistant/run", json=payload, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["request_id"] == "tg:1004"
    assert data["status"] == "completed"
    assert data["answer"] is not None
    assert data["error_code"] is None
