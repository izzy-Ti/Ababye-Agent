import os
import pytest
from pathlib import Path
from backend.app.mcp_manager import MCPServerSession


def test_dockerfile_pinned_non_root_and_mcp_setup():
    dockerfile_path = Path(__file__).resolve().parent.parent.parent / "Dockerfile"
    assert dockerfile_path.exists(), "Dockerfile must exist"
    
    content = dockerfile_path.read_text(encoding="utf-8")
    
    # Pinned container image
    assert "FROM python:3.12-slim" in content, "Must use pinned python base image"
    
    # Non-root user
    assert "useradd" in content and "appuser" in content, "Must create appuser"
    assert "USER appuser" in content, "Must switch to non-root appuser"
    
    # Pinned dependencies
    assert "requirements.lock" in content, "Must install from pinned requirements.lock"
    
    # MCP configuration and data directory
    assert "mcp_config.json" in content, "Must copy mcp_config.json"
    assert "mkdir -p /app/data" in content, "Must create /app/data directory for storage"


def test_mcp_subprocess_secret_isolation(monkeypatch):
    # Set simulated sensitive backend secrets and server-specific credential
    monkeypatch.setenv("EDGE_BACKEND_SECRET", "super-secret-edge-token")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "super-secret-internal-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:secret-tg-token")
    monkeypatch.setenv("CALLBACK_SIGNING_SECRET", "super-secret-hmac-key")
    monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini-key")
    monkeypatch.setenv("ALLOWED_MCP_API_KEY", "mcp-service-key-1234")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    
    # Config attempting to include both normal and sensitive vars
    server_config = {
        "command": "python",
        "args": ["-c", "print('hello')"],
        "permitted_env_vars": [
            "PATH",
            "ALLOWED_MCP_API_KEY",
            "EDGE_BACKEND_SECRET",      # should be blocked
            "INTERNAL_SERVICE_TOKEN",   # should be blocked
            "TELEGRAM_BOT_TOKEN",       # should be blocked
            "CALLBACK_SIGNING_SECRET",  # should be blocked
            "GEMINI_API_KEY",           # should be blocked
        ],
        "env": {
            "SAFE_CUSTOM_CONFIG": "custom_val",
            "TELEGRAM_BOT_TOKEN": "override_leak_attempt" # should be blocked
        }
    }
    
    session = MCPServerSession("secure_tool", server_config)
    env = session._build_sanitized_environment()
    
    # Allowed server-specific variables are passed
    assert env.get("ALLOWED_MCP_API_KEY") == "mcp-service-key-1234"
    assert env.get("SAFE_CUSTOM_CONFIG") == "custom_val"
    assert "PATH" in env
    
    # Sensitive backend secrets are NEVER passed
    assert "EDGE_BACKEND_SECRET" not in env
    assert "INTERNAL_SERVICE_TOKEN" not in env
    assert "CALLBACK_SIGNING_SECRET" not in env
    assert "GEMINI_API_KEY" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env or env["TELEGRAM_BOT_TOKEN"] != "override_leak_attempt"
    assert "TELEGRAM_BOT_TOKEN" not in env
