import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, status
from fastapi.responses import JSONResponse

import uuid
from backend.app.schemas import (
    RunRequest,
    RunResponse,
    HealthResponse,
    JobRequest,
    JobResponse,
)
from backend.app.auth import verify_bearer_token, verify_allowed_user
from backend.app.agent import agent_runner
from backend.app.mcp_manager import mcp_manager
from backend.app.queue import durable_queue, queue_worker, JobRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up backend application...")
    await mcp_manager.initialize()
    await queue_worker.start()
    yield
    logger.info("Shutting down backend application...")
    await queue_worker.stop()
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


@app.post(
    "/v1/assistant/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Assistant"],
)
async def submit_job(
    request: JobRequest,
    _auth: str = Depends(verify_bearer_token),
):
    """
    Durably records and queues the job, then returns 202 with job_id.
    """
    verify_allowed_user(request.user_id)
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    job_record = JobRecord(
        job_id=job_id,
        update_id=request.update_id,
        request_id=request.request_id,
        user_id=request.user_id,
        chat_id=request.chat_id,
        message_id=request.message_id,
        text=request.text,
        voice_metadata=request.voice.model_dump() if request.voice else None,
        forward_info=request.forward_info,
        media_type=request.media_type,
        callback_url=request.callback_url,
    )
    enqueued = await durable_queue.enqueue(job_record)
    return JobResponse(
        job_id=enqueued.job_id,
        request_id=enqueued.request_id,
        status=enqueued.status,
        answer=enqueued.answer,
        error_code=enqueued.error_code,
    )


@app.get(
    "/v1/assistant/jobs/{job_id}",
    response_model=JobResponse,
    tags=["Assistant"],
)
async def get_job(
    job_id: str,
    _auth: str = Depends(verify_bearer_token),
):
    job = await durable_queue.get_job(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return JobResponse(
        job_id=job.job_id,
        request_id=job.request_id,
        status=job.status,
        answer=job.answer,
        error_code=job.error_code,
    )


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("BACKEND_HOST", "0.0.0.0")
    port = int(os.environ.get("BACKEND_PORT", "8000"))
    uvicorn.run("backend.app.main:app", host=host, port=port, reload=False)
