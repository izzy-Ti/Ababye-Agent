import time
from mcp.server.mcpserver import MCPServer

app = MCPServer("echo_server")


@app.tool()
def echo(message: str) -> str:
    """Echoes the input message back."""
    return f"Echo: {message}"


@app.tool()
def add(a: int, b: int) -> int:
    """Adds two integers."""
    return a + b


@app.tool()
def slow_echo(message: str, delay_seconds: float = 2.0) -> str:
    """Delays execution by delay_seconds then returns message."""
    time.sleep(delay_seconds)
    return f"Delayed: {message}"


if __name__ == "__main__":
    app.run("stdio")
