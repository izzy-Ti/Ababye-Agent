import os
import time
import sqlite3
import asyncio
from typing import Optional, Dict, Any, List
from pathlib import Path


class RequestRecord:
    def __init__(
        self,
        request_id: str,
        update_id: int,
        user_id: int,
        chat_id: int,
        message_id: int,
        text: str,
        status: str = "in_progress",
        answer: Optional[str] = None,
        error_code: Optional[str] = None,
        tool_calls_count: int = 0,
        created_at: Optional[float] = None,
        updated_at: Optional[float] = None,
    ):
        self.request_id = request_id
        self.update_id = update_id
        self.user_id = user_id
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.status = status
        self.answer = answer
        self.error_code = error_code
        self.tool_calls_count = tool_calls_count
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "update_id": self.update_id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "text": self.text,
            "status": self.status,
            "answer": self.answer,
            "error_code": self.error_code,
            "tool_calls_count": self.tool_calls_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Storage:
    """
    Authoritative shared storage layer for request state persistence,
    deduplication across container restarts, and chat context.
    Backed by SQLite for durability.
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
        # Enable WAL mode for concurrent read/write performance
        if self._db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS requests (
                        update_id INTEGER PRIMARY KEY,
                        request_id TEXT NOT NULL,
                        user_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        text TEXT NOT NULL,
                        status TEXT NOT NULL,
                        answer TEXT,
                        error_code TEXT,
                        tool_calls_count INTEGER DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_history_chat_id 
                    ON chat_history (chat_id, created_at)
                    """
                )
        finally:
            conn.close()

    async def get_by_update_id(self, update_id: int) -> Optional[RequestRecord]:
        async with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT * FROM requests WHERE update_id = ?", (update_id,)
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return RequestRecord(
                    request_id=row["request_id"],
                    update_id=row["update_id"],
                    user_id=row["user_id"],
                    chat_id=row["chat_id"],
                    message_id=row["message_id"],
                    text=row["text"],
                    status=row["status"],
                    answer=row["answer"],
                    error_code=row["error_code"],
                    tool_calls_count=row["tool_calls_count"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            finally:
                conn.close()

    async def get_by_request_id(self, request_id: str) -> Optional[RequestRecord]:
        async with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT * FROM requests WHERE request_id = ?", (request_id,)
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return RequestRecord(
                    request_id=row["request_id"],
                    update_id=row["update_id"],
                    user_id=row["user_id"],
                    chat_id=row["chat_id"],
                    message_id=row["message_id"],
                    text=row["text"],
                    status=row["status"],
                    answer=row["answer"],
                    error_code=row["error_code"],
                    tool_calls_count=row["tool_calls_count"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            finally:
                conn.close()

    async def save_request(self, record: RequestRecord) -> None:
        async with self._lock:
            record.updated_at = time.time()
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO requests (
                            update_id, request_id, user_id, chat_id, message_id,
                            text, status, answer, error_code, tool_calls_count,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(update_id) DO UPDATE SET
                            status = excluded.status,
                            answer = excluded.answer,
                            error_code = excluded.error_code,
                            tool_calls_count = excluded.tool_calls_count,
                            updated_at = excluded.updated_at
                        """,
                        (
                            record.update_id,
                            record.request_id,
                            record.user_id,
                            record.chat_id,
                            record.message_id,
                            record.text,
                            record.status,
                            record.answer,
                            record.error_code,
                            record.tool_calls_count,
                            record.created_at,
                            record.updated_at,
                        ),
                    )
            finally:
                conn.close()

    async def get_chat_history(
        self, chat_id: int, limit: int = 10
    ) -> List[Dict[str, str]]:
        async with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """
                    SELECT role, content FROM chat_history
                    WHERE chat_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (chat_id, limit),
                )
                rows = cursor.fetchall()
                # Return in chronological order
                return [
                    {"role": row["role"], "content": row["content"]}
                    for row in reversed(rows)
                ]
            finally:
                conn.close()

    async def append_chat_message(
        self, chat_id: int, role: str, content: str
    ) -> None:
        async with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO chat_history (chat_id, role, content, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (chat_id, role, content, time.time()),
                    )
            finally:
                conn.close()

    async def clear_chat_history(self, chat_id: int) -> None:
        async with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        "DELETE FROM chat_history WHERE chat_id = ?", (chat_id,)
                    )
            finally:
                conn.close()


# Singleton storage instance
storage = Storage()
