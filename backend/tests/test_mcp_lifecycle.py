import sys
import asyncio
import pytest
from pathlib import Path
from backend.app.mcp_manager import MCPManager, MCPServerSession


@pytest.fixture
def mcp_config_path(tmp_path):
    config_file = tmp_path / "mcp_config_test.json"
    content = f"""
    {{
      "mcpServers": {{
        "echo": {{
          "command": "{sys.executable.replace('\\', '/')}",
          "args": ["-m", "backend.app.mcp_servers.echo_server"],
          "env": {{}},
          "permitted_env_vars": ["PATH", "SYSTEMROOT", "PYTHONPATH", "TEMP", "TMP"],
          "tool_allowlist": ["echo", "add", "slow_echo"]
        }}
      }}
    }}
    """
    config_file.write_text(content, encoding="utf-8")
    return str(config_file)


@pytest.mark.asyncio
async def test_mcp_initialization_discovery_and_execution(mcp_config_path):
    """
    Verify server initialization, tool discovery, namespacing, and successful tool execution.
    """
    manager = MCPManager(config_path=mcp_config_path)
    try:
        await manager.initialize()
        tools = await manager.get_tools()
        names = [t.name for t in tools]

        # Check namespacing: echo_echo, echo_add, echo_slow_echo
        assert "echo_echo" in names
        assert "echo_add" in names
        assert "echo_slow_echo" in names

        # Execute echo_echo tool successfully
        res = await manager.execute_tool("echo_echo", {"message": "Test MCP"})
        assert res["is_error"] is False
        assert "Echo: Test MCP" in res["text"]

        # Execute echo_add tool successfully
        add_res = await manager.execute_tool("echo_add", {"a": 15, "b": 35})
        assert add_res["is_error"] is False
        assert "50" in add_res["text"]

    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_allowlist_enforcement(tmp_path):
    """
    Verify that tools not in tool_allowlist are filtered out and cannot be executed.
    """
    config_file = tmp_path / "mcp_config_restricted.json"
    content = f"""
    {{
      "mcpServers": {{
        "echo": {{
          "command": "{sys.executable.replace('\\', '/')}",
          "args": ["-m", "backend.app.mcp_servers.echo_server"],
          "tool_allowlist": ["echo"]
        }}
      }}
    }}
    """
    config_file.write_text(content, encoding="utf-8")

    manager = MCPManager(config_path=str(config_file))
    try:
        await manager.initialize()
        tools = await manager.get_tools()
        names = [t.name for t in tools]

        # 'echo_echo' should be present, but 'echo_add' must be absent
        assert "echo_echo" in names
        assert "echo_add" not in names

        # Calling unapproved tool raises ValueError
        with pytest.raises(ValueError, match="not in the approved allowlist"):
            await manager.execute_tool("echo_add", {"a": 1, "b": 2})

    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_timeout_closes_session_and_reaps_process(mcp_config_path):
    """
    Verify that per-tool timeout aborts execution, closes session, and reaps child process.
    """
    manager = MCPManager(config_path=mcp_config_path)
    try:
        await manager.initialize()

        # Call slow_echo (delays 2.0s) with timeout_seconds=0.2s
        with pytest.raises(TimeoutError):
            await manager.execute_tool(
                "echo_slow_echo", {"message": "Hurry", "delay_seconds": 2.0}, timeout_seconds=0.2
            )

        # Server session must be marked closed / stopped after timeout
        server = manager._servers.get("echo")
        assert server is not None
        assert server.is_connected is False
        assert server._worker_task is None

    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_cancellation_closes_session(mcp_config_path):
    """
    Verify that cancelling tool execution cleanly stops session and reaps process.
    """
    manager = MCPManager(config_path=mcp_config_path)
    try:
        await manager.initialize()
        server = manager._servers.get("echo")
        assert server is not None

        task = asyncio.create_task(
            manager.execute_tool(
                "echo_slow_echo", {"message": "Cancel me", "delay_seconds": 3.0}, timeout_seconds=10.0
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert server.is_connected is False
        assert server._worker_task is None

    finally:
        await manager.shutdown()
