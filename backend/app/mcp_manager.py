import os
import sys
import json
import logging
import asyncio
from typing import Dict, List, Any, Optional
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


class MCPToolDefinition:
    def __init__(
        self,
        name: str,
        server_name: str,
        original_name: str,
        description: str,
        input_schema: Dict[str, Any],
    ):
        self.name = name  # namespaced name: {server_name}_{original_name}
        self.server_name = server_name
        self.original_name = original_name
        self.description = description or ""
        self.input_schema = input_schema or {"type": "object", "properties": {}}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "server_name": self.server_name,
            "original_name": self.original_name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class MCPServerSession:
    """
    Manages the lifecycle of a single MCP server subprocess and stdio transport.
    Uses a dedicated worker task so that AnyIO cancel scopes and streams are always
    entered and exited within the same task context, guaranteeing leak-free process reaping.
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.config = config
        self.command = config.get("command", sys.executable)
        self.args = config.get("args", [])
        self.fixed_env = config.get("env", {})
        self.permitted_env_vars = set(
            config.get(
                "permitted_env_vars",
                ["PATH", "SYSTEMROOT", "PYTHONPATH", "TEMP", "TMP", "HOME", "USERPROFILE"],
            )
        )
        self.tool_allowlist = set(config.get("tool_allowlist", []))

        self.tools: Dict[str, MCPToolDefinition] = {}
        self.is_connected = False

        self._queue: asyncio.Queue = asyncio.Queue()
        self._ready_event = asyncio.Event()
        self._init_error: Optional[Exception] = None
        self._worker_task: Optional[asyncio.Task] = None

    def _build_sanitized_environment(self) -> Dict[str, str]:
        """
        Build an environment containing only permitted environment variables
        from system env, combined with fixed server-specific variables.
        Never allows untrusted or unallowlisted environment variables.
        """
        env: Dict[str, str] = {}
        for var in self.permitted_env_vars:
            val = os.environ.get(var)
            if val is not None:
                env[var] = val

        # Merge fixed trusted environment
        for k, v in self.fixed_env.items():
            env[k] = str(v)

        return env

    async def start(self) -> None:
        """
        Start server worker task, initialize ClientSession, and discover allowed tools.
        """
        if self.is_connected:
            return

        self._ready_event.clear()
        self._init_error = None
        self._worker_task = asyncio.create_task(self._worker_loop())

        # Wait for worker to finish initialization and discovery
        await self._ready_event.wait()
        if self._init_error:
            await self.stop()
            raise self._init_error

    async def _worker_loop(self) -> None:
        sanitized_env = self._build_sanitized_environment()
        cmd = self.command
        if cmd == "python":
            cmd = sys.executable

        server_params = StdioServerParameters(
            command=cmd,
            args=self.args,
            env=sanitized_env,
        )

        try:
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    # Discover and filter tools
                    tools_response = await session.list_tools()
                    self.tools.clear()

                    for tool in tools_response.tools:
                        orig_name = tool.name
                        if self.tool_allowlist and orig_name not in self.tool_allowlist:
                            logger.info(
                                f"Skipping tool '{orig_name}' on server '{self.name}' (not in allowlist)"
                            )
                            continue

                        namespaced_name = f"{self.name}_{orig_name}"
                        schema = (
                            tool.inputSchema
                            if hasattr(tool, "inputSchema")
                            else getattr(tool, "input_schema", {"type": "object", "properties": {}})
                        )

                        self.tools[namespaced_name] = MCPToolDefinition(
                            name=namespaced_name,
                            server_name=self.name,
                            original_name=orig_name,
                            description=tool.description or "",
                            input_schema=schema,
                        )

                    self.is_connected = True
                    self._ready_event.set()
                    logger.info(
                        f"MCP server '{self.name}' initialized with tools: {list(self.tools.keys())}"
                    )

                    # Worker message dispatch loop
                    while True:
                        req = await self._queue.get()
                        if req is None:
                            # Shutdown signal
                            break

                        orig_tool_name, arguments, fut = req
                        if fut.cancelled():
                            continue

                        try:
                            result = await session.call_tool(orig_tool_name, arguments)
                            if not fut.cancelled():
                                fut.set_result(result)
                        except Exception as e:
                            if not fut.cancelled():
                                fut.set_exception(e)

        except Exception as e:
            logger.exception(f"Exception in MCP server '{self.name}' worker: {e}")
            self._init_error = e
            self._ready_event.set()
        finally:
            self.is_connected = False
            self.tools.clear()
            logger.info(f"MCP server '{self.name}' worker loop finished and reaped child process.")

    async def call_tool(
        self,
        original_tool_name: str,
        arguments: Dict[str, Any],
        timeout_seconds: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Call tool on active session with timeout, cancellation safety, and error handling.
        """
        if not self.is_connected:
            raise RuntimeError(f"MCP server '{self.name}' is not running")

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        await self._queue.put((original_tool_name, arguments, fut))

        try:
            result = await asyncio.wait_for(fut, timeout=timeout_seconds)

            # Extract text content
            text_content = ""
            if hasattr(result, "content") and result.content:
                parts = []
                for item in result.content:
                    if hasattr(item, "text"):
                        parts.append(item.text)
                    elif isinstance(item, dict) and "text" in item:
                        parts.append(item["text"])
                text_content = "\n".join(parts)

            is_error = getattr(result, "isError", False)
            return {
                "text": text_content,
                "is_error": is_error,
            }

        except TimeoutError:
            logger.error(
                f"Tool '{original_tool_name}' on server '{self.name}' timed out after {timeout_seconds}s"
            )
            fut.cancel()
            # Stop server on timeout to ensure subprocess is reaped
            await self.stop()
            raise TimeoutError(
                f"Tool execution timed out after {timeout_seconds} seconds"
            )
        except asyncio.CancelledError:
            logger.warning(
                f"Tool '{original_tool_name}' on server '{self.name}' was cancelled"
            )
            fut.cancel()
            await asyncio.shield(self.stop())
            raise
        except Exception as e:
            logger.exception(
                f"Tool '{original_tool_name}' on server '{self.name}' failed: {e}"
            )
            raise e

    async def stop(self) -> None:
        """
        Cleanly closes session, terminates transport streams, and reaps child process.
        """
        self.is_connected = False
        if self._worker_task and not self._worker_task.done():
            # Signal worker to stop
            try:
                self._queue.put_nowait(None)
            except Exception:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(self._worker_task), timeout=1.5)
            except (TimeoutError, asyncio.CancelledError, Exception):
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except (asyncio.CancelledError, Exception):
                    pass
        self._worker_task = None
        self.tools.clear()


class MCPManager:
    """
    Subprocess lifecycle manager for all trusted MCP servers:
    - Loads servers, commands, fixed arguments, permitted env vars, and tool allowlists.
    - Discovers and namespaces approved tools.
    - Manages subprocess lifecycle (start, call, timeout, cancellation, shutdown, reaping).
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.environ.get(
            "MCP_CONFIG_PATH", "./mcp_config.json"
        )
        self._servers: Dict[str, MCPServerSession] = {}
        self._tools: Dict[str, MCPToolDefinition] = {}
        self._initialized = False

    def _load_config(self) -> Dict[str, Any]:
        path = Path(self.config_path)
        if not path.exists():
            alt_path = Path(__file__).resolve().parent.parent.parent / "mcp_config.json"
            if alt_path.exists():
                path = alt_path

        if not path.exists():
            logger.warning(f"MCP configuration file not found at {path}")
            return {"mcpServers": {}}

        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading MCP config from {path}: {e}")
            return {"mcpServers": {}}

    async def initialize(self) -> None:
        """
        Initialize all configured MCP servers and discover their approved tools.
        """
        logger.info("Initializing MCP manager and discovering tools...")
        config = self._load_config()
        mcp_servers = config.get("mcpServers", {})

        self._tools.clear()

        for server_name, server_cfg in mcp_servers.items():
            try:
                server = MCPServerSession(server_name, server_cfg)
                await server.start()
                self._servers[server_name] = server

                # Aggregate discovered tools
                for namespaced_name, tool_def in server.tools.items():
                    self._tools[namespaced_name] = tool_def

            except Exception as e:
                logger.error(f"Could not initialize MCP server '{server_name}': {e}")

        self._initialized = True
        logger.info(f"MCP Manager ready with {len(self._tools)} approved tools.")

    async def get_tools(self) -> List[MCPToolDefinition]:
        return list(self._tools.values())

    async def get_tool(self, namespaced_name: str) -> Optional[MCPToolDefinition]:
        return self._tools.get(namespaced_name)

    async def execute_tool(
        self,
        namespaced_tool_name: str,
        arguments: Dict[str, Any],
        timeout_seconds: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Execute an approved tool using its namespaced identifier.
        """
        tool_def = self._tools.get(namespaced_tool_name)
        if not tool_def:
            raise ValueError(
                f"Tool '{namespaced_tool_name}' is not in the approved allowlist"
            )

        server = self._servers.get(tool_def.server_name)
        if not server or not server.is_connected:
            raise RuntimeError(
                f"MCP server '{tool_def.server_name}' is not available"
            )

        return await server.call_tool(
            original_tool_name=tool_def.original_name,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
        )

    async def shutdown(self) -> None:
        """
        Gracefully close all active MCP sessions and reap child processes.
        """
        logger.info("Shutting down all MCP server sessions...")
        for name, server in list(self._servers.items()):
            await server.stop()
        self._servers.clear()
        self._tools.clear()
        self._initialized = False


mcp_manager = MCPManager()
