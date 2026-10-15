import os
import time
import json
import uuid
import hmac
import hashlib
import asyncio
import logging
import sqlite3
import httpx
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path

from backend.app.schemas import JobRequest, JobResponse, CompletionEvent, RunRequest
from backend.app.agent import agent_runner, AgentRunner

logger = logging.getLogger("queue")


class JobRecord:
    def __init__(
        self,
        job_id: str,
        update_id: int,
        request_id: str,
        user_id: int,
        chat_id: int,
        message_id: int,
        text: str,
        status: str = "queued",
        voice_metadata: Optional[Dict[str, Any]] = None,
        forward_info: Optional[Dict[str, Any]] = None,
        media_type: Optional[str] = None,
        callback_url: Optional[str] = None,
        attempts: int = 0,
        max_retries: int = 3,
        answer: Optional[str] = None,
        error_code: Optional[str] = None,
        callback_status: str = "pending",
        callback_attempts: int = 0,
        leased_until: Optional[float] = None,
        created_at: Optional[float] = None,
        updated_at: Optional[float] = None,
    ):
        self.job_id = job_id
        self.update_id = update_id
        self.request_id = request_id
        self.user_id = user_id
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.status = status
        self.voice_metadata = voice_metadata
        self.forward_info = forward_info
        self.media_type = media_type
        self.callback_url = callback_url
        self.attempts = attempts
        self.max_retries = max_retries
        self.answer = answer
        self.error_code = error_code
        self.callback_status = callback_status
        self.callback_attempts = callback_attempts
        self.leased_until = leased_until
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "update_id": self.update_id,
            "request_id": self.request_id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "text": self.text,
            "status": self.status,
            "voice_metadata": self.voice_metadata,
            "forward_info": self.forward_info,
            "media_type": self.media_type,
            "callback_url": self.callback_url,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "answer": self.answer,
            "error_code": self.error_code,
            "callback_status": self.callback_status,
            "callback_attempts": self.callback_attempts,
            "leased_until": self.leased_until,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DurableJobQueue:
    """
    Durable SQLite-backed job queue supporting atomic lease, retry with bounded backoff,
    and worker crash recovery (resumes expired leases across restarts).
    """

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or os.environ.get(
            "SHARED_STORAGE_URL", "sqlite:///./data/assistant.db"
        )
        self._db_path = self._parse_db_path(self.db_url)
        self._lock = asyncio.Lock()
        self._init_db()

    def _parse_db_path(self, url: str) -> str:
        if url.startswith("sqlite+aiosqlite:///"):
            path_str = url.replace("sqlite+aiosqlite:///", "")
        elif url.startswith("sqlite:///"):
            path_str = url.replace("sqlite:///", "")
        elif url.startswith("memory://") or url == ":memory:":
            return ":memory:"
        else:
            path_str = url

        if path_str != ":memory:":
            db_file = Path(path_str)
            db_file.parent.mkdir(parents=True, exist_ok=True)
            return str(db_file.resolve())
        return ":memory:"

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if self._db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS durable_jobs (
                        job_id TEXT PRIMARY KEY,
                        update_id INTEGER UNIQUE NOT NULL,
                        request_id TEXT NOT NULL,
                        user_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        text TEXT NOT NULL,
                        status TEXT NOT NULL,
                        voice_metadata TEXT,
                        forward_info TEXT,
                        media_type TEXT,
                        callback_url TEXT,
                        attempts INTEGER DEFAULT 0,
                        max_retries INTEGER DEFAULT 3,
                        answer TEXT,
                        error_code TEXT,
                        callback_status TEXT DEFAULT 'pending',
                        callback_attempts INTEGER DEFAULT 0,
                        leased_until REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_durable_jobs_status
                    ON durable_jobs (status, leased_until, attempts)
                    """
                )
        finally:
            conn.close()

    async def enqueue(self, job: JobRecord) -> JobRecord:
        async with self._lock:
            conn = self._get_connection()
            try:
                # Check deduplication on update_id
                cursor = conn.execute(
                    "SELECT * FROM durable_jobs WHERE update_id = ?", (job.update_id,)
                )
                row = cursor.fetchone()
                if row:
                    logger.info(f"Reusing existing queued job for update_id={job.update_id}")
                    return self._row_to_record(row)

                with conn:
                    conn.execute(
                        """
                        INSERT INTO durable_jobs (
                            job_id, update_id, request_id, user_id, chat_id, message_id,
                            text, status, voice_metadata, forward_info, media_type,
                            callback_url, attempts, max_retries, answer, error_code,
                            callback_status, callback_attempts, leased_until, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job.job_id,
                            job.update_id,
                            job.request_id,
                            job.user_id,
                            job.chat_id,
                            job.message_id,
                            job.text,
                            job.status,
                            json.dumps(job.voice_metadata) if job.voice_metadata else None,
                            json.dumps(job.forward_info) if job.forward_info else None,
                            job.media_type,
                            job.callback_url,
                            job.attempts,
                            job.max_retries,
                            job.answer,
                            job.error_code,
                            job.callback_status,
                            job.callback_attempts,
                            job.leased_until,
                            job.created_at,
                            job.updated_at,
                        ),
                    )
                return job
            finally:
                conn.close()

    async def get_job(self, job_id: str) -> Optional[JobRecord]:
        async with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT * FROM durable_jobs WHERE job_id = ?", (job_id,)
                )
                row = cursor.fetchone()
                return self._row_to_record(row) if row else None
            finally:
                conn.close()

    async def lease_next_job(self, lease_seconds: float = 60.0) -> Optional[JobRecord]:
        """
        Atomically leases the next queued job or an abandoned running job whose lease expired.
        Worker restart automatically resumes or safely retries the job.
        """
        async with self._lock:
            conn = self._get_connection()
            try:
                now = time.time()
                with conn:
                    # Find candidate job
                    cursor = conn.execute(
                        """
                        SELECT * FROM durable_jobs
                        WHERE (status = 'queued' OR (status = 'running' AND leased_until < ?))
                          AND attempts < max_retries
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (now,),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return None

                    job_id = row["job_id"]
                    new_attempts = row["attempts"] + 1
                    leased_until = now + lease_seconds

                    # Atomically update
                    conn.execute(
                        """
                        UPDATE durable_jobs
                        SET status = 'running',
                            attempts = ?,
                            leased_until = ?,
                            updated_at = ?
                        WHERE job_id = ?
                        """,
                        (new_attempts, leased_until, now, job_id),
                    )

                    # Re-fetch updated record
                    cursor2 = conn.execute(
                        "SELECT * FROM durable_jobs WHERE job_id = ?", (job_id,)
                    )
                    updated_row = cursor2.fetchone()
                    return self._row_to_record(updated_row)
            finally:
                conn.close()

    async def complete_job(
        self, job_id: str, answer: str, error_code: Optional[str] = None, success: bool = True
    ) -> None:
        async with self._lock:
            conn = self._get_connection()
            try:
                now = time.time()
                status = "completed" if success else "failed"
                with conn:
                    conn.execute(
                        """
                        UPDATE durable_jobs
                        SET status = ?,
                            answer = ?,
                            error_code = ?,
                            leased_until = NULL,
                            updated_at = ?
                        WHERE job_id = ?
                        """,
                        (status, answer, error_code, now, job_id),
                    )
            finally:
                conn.close()

    async def update_callback_state(
        self, job_id: str, callback_status: str, callback_attempts: int
    ) -> None:
        async with self._lock:
            conn = self._get_connection()
            try:
                now = time.time()
                with conn:
                    conn.execute(
                        """
                        UPDATE durable_jobs
                        SET callback_status = ?,
                            callback_attempts = ?,
                            updated_at = ?
                        WHERE job_id = ?
                        """,
                        (callback_status, callback_attempts, now, job_id),
                    )
            finally:
                conn.close()

    def _row_to_record(self, row: sqlite3.Row) -> JobRecord:
        voice_meta = None
        if "voice_metadata" in row.keys() and row["voice_metadata"]:
            try:
                voice_meta = json.loads(row["voice_metadata"])
            except Exception:
                voice_meta = None

        forward_info = None
        if "forward_info" in row.keys() and row["forward_info"]:
            try:
                forward_info = json.loads(row["forward_info"])
            except Exception:
                forward_info = None

        return JobRecord(
            job_id=row["job_id"],
            update_id=row["update_id"],
            request_id=row["request_id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            text=row["text"],
            status=row["status"],
            voice_metadata=voice_meta,
            forward_info=forward_info,
            media_type=row["media_type"] if "media_type" in row.keys() else None,
            callback_url=row["callback_url"] if "callback_url" in row.keys() else None,
            attempts=row["attempts"],
            max_retries=row["max_retries"],
            answer=row["answer"],
            error_code=row["error_code"],
            callback_status=row["callback_status"],
            callback_attempts=row["callback_attempts"],
            leased_until=row["leased_until"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def compute_callback_hmac(body_bytes: bytes, timestamp: int, secret: str) -> str:
    """
    Computes standard timestamped HMAC signature: HMAC-SHA256(secret, "{timestamp}.{body}")
    """
    message = f"{timestamp}.".encode("utf-8") + body_bytes
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


class QueueWorker:
    """
    Queue worker executing model + MCP engine for durable jobs and delivering
    HMAC-signed completion events with bounded exponential backoff.
    """

    def __init__(
        self,
        queue: DurableJobQueue,
        runner: Optional[AgentRunner] = None,
        secret: Optional[str] = None,
        default_callback_url: Optional[str] = None,
    ):
        self.queue = queue
        self.runner = runner or agent_runner
        self.secret = secret or os.environ.get("EDGE_BACKEND_SECRET", "mock-backend-secret")
        self.default_callback_url = default_callback_url or os.environ.get(
            "EDGE_CALLBACK_URL", "http://127.0.0.1:8080/v1/assistant/callback"
        )
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._worker_loop())
        logger.info("Durable queue worker started.")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Durable queue worker stopped.")

    async def _worker_loop(self) -> None:
        while self._running:
            try:
                job = await self.queue.lease_next_job(lease_seconds=60.0)
                if not job:
                    await asyncio.sleep(0.5)
                    continue

                await self.process_job(job)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in queue worker loop: {e}")
                await asyncio.sleep(1.0)

    async def process_job(self, job: JobRecord) -> None:
        logger.info(f"Processing durable job {job.job_id} (attempt {job.attempts}/{job.max_retries})")
        # Run MCP + Gemini engine using standard RunRequest
        run_req = RunRequest(
            request_id=job.request_id,
            update_id=job.update_id,
            user_id=job.user_id,
            chat_id=job.chat_id,
            message_id=job.message_id,
            text=job.text,
            forward_info=job.forward_info,
            media_type=job.media_type,
        )

        try:
            result = await self.runner.run(run_req)
            success = result.status == "completed"
            answer = result.answer or "Processing complete."
            error_code = result.error_code

            # Durably record completion
            await self.queue.complete_job(
                job_id=job.job_id,
                answer=answer,
                error_code=error_code,
                success=success,
            )

            # Send signed callback event to edge
            callback_url = job.callback_url or self.default_callback_url
            if callback_url:
                await self.deliver_callback_with_backoff(
                    callback_url=callback_url,
                    job=job,
                    answer=answer,
                    status=result.status if success else "failed",
                    error_code=error_code,
                )

        except Exception as e:
            logger.exception(f"Error processing job {job.job_id}: {e}")
            await self.queue.complete_job(
                job_id=job.job_id,
                answer=str(e),
                error_code="JOB_PROCESSING_FAILED",
                success=False,
            )

    async def deliver_callback_with_backoff(
        self,
        callback_url: str,
        job: JobRecord,
        answer: str,
        status: str,
        error_code: Optional[str] = None,
        max_attempts: int = 3,
    ) -> bool:
        """
        POSTs a signed completion event to edge endpoint with bounded exponential backoff.
        Separate tool execution from delivery retries: tools run during job execution and are never rerun here.
        """
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        now = time.time()
        timestamp = int(now)

        event_payload = {
            "event_id": event_id,
            "job_id": job.job_id,
            "request_id": job.request_id,
            "user_id": job.user_id,
            "chat_id": job.chat_id,
            "message_id": job.message_id,
            "status": status,
            "answer": answer,
            "error_code": error_code,
            "created_at": now,
        }

        body_bytes = json.dumps(event_payload).encode("utf-8")
        signature = compute_callback_hmac(body_bytes, timestamp, self.secret)
        headers = {
            "Content-Type": "application/json",
            "X-Assistant-Signature": f"t={timestamp},v1={signature}",
            "X-Assistant-Timestamp": str(timestamp),
        }

        attempts = 0
        backoff_delays = [0.1, 0.2, 0.5]  # Fast bounded backoff

        while attempts < max_attempts:
            attempts += 1
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(callback_url, content=body_bytes, headers=headers)
                    if resp.status_code in (200, 202):
                        logger.info(
                            f"Callback for job {job.job_id} delivered successfully on attempt {attempts}"
                        )
                        await self.queue.update_callback_state(job.job_id, "delivered", attempts)
                        return True
                    else:
                        logger.warning(
                            f"Callback failed with HTTP {resp.status_code} (attempt {attempts}/{max_attempts})"
                        )
            except Exception as e:
                logger.warning(f"Callback delivery error on attempt {attempts}: {e}")

            if attempts < max_attempts:
                delay = backoff_delays[min(attempts - 1, len(backoff_delays) - 1)]
                await asyncio.sleep(delay)

        logger.error(f"Callback delivery for job {job.job_id} failed after {max_attempts} attempts.")
        await self.queue.update_callback_state(job.job_id, "failed", attempts)
        return False


# Singleton instances
durable_queue = DurableJobQueue()
queue_worker = QueueWorker(queue=durable_queue)
