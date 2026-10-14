import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, status
from fastapi.responses import JSONResponse

from backend.app.schemas import RunRequest, RunResponse, HealthResponse
from backend.app.auth import verify_bearer_token, verify_allowed_user
from backend.app.agent import agent_runner
from backend.app.mcp_manager import mcp_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up backend application...")
    await mcp_manager.initialize()
    yield
    logger.info("Shutting down backend application...")
    await mcp_manager.shutdown()


app = FastAPI(
    title="Telegram Assistant Backend",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse, tags=["Health"])
@app.get("/healthz", response_model=HealthResponse, tags=["Health"])
async def health_check():
    return HealthResponse(status="ok", version="0.1.0")


@app.post(
    "/v1/assistant/run",
    response_model=RunResponse,
    status_code=status.HTTP_200_OK,
    tags=["Assistant"],
)
async def run_assistant(
    request: RunRequest,
    _auth: str = Depends(verify_bearer_token),
):
    # Verify sender identity against allowlist (403 if disallowed)
    verify_allowed_user(request.user_id)

    # Execute agent loop
    result = await agent_runner.run(request)
    return result


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("BACKEND_HOST", "0.0.0.0")
    port = int(os.environ.get("BACKEND_PORT", "8000"))
    uvicorn.run("backend.app.main:app", host=host, port=port, reload=False)
