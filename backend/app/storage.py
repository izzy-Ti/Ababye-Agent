import os
import time
import json
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
        bounded_history: Optional[List[Dict[str, Any]]] = None,
        forward_info: Optional[Dict[str, Any]] = None,
        media_type: Optional[str] = None,
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
        self.bounded_history = bounded_history or []
        self.forward_info = forward_info
        self.media_type = media_type
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
            "bounded_history": self.bounded_history,
            "forward_info": self.forward_info,
            "media_type": self.media_type,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ActionRecord:
    """
    Represents a proposed write action with validated arguments,
    a short-lived confirmation token, and an idempotency key.
    """

    def __init__(
        self,
        action_id: str,
        idempotency_key: str,
        chat_id: int,
        user_id: int,
        tool_name: str,
        arguments: Dict[str, Any],
        confirmation_token: str,
        status: str = "pending",
        created_at: Optional[float] = None,
        expires_at: Optional[float] = None,
        executed_at: Optional[float] = None,
        result: Optional[str] = None,
    ):
        self.action_id = action_id
        self.idempotency_key = idempotency_key
        self.chat_id = chat_id
        self.user_id = user_id
        self.tool_name = tool_name
        self.arguments = arguments
        self.confirmation_token = confirmation_token
        self.status = status  # 'pending', 'in_progress', 'executed', 'expired', 'failed'
        self.created_at = created_at or time.time()
        self.expires_at = expires_at or (self.created_at + 300.0)  # default 5 minutes
        self.executed_at = executed_at
        self.result = result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "idempotency_key": self.idempotency_key,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "confirmation_token": self.confirmation_token,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "executed_at": self.executed_at,
            "result": self.result,
        }


class Storage:
    """
    Authoritative shared storage layer for request state persistence,
    deduplication across container restarts, chat context, and write action records.
    Backed by SQLite with WAL mode for durability and concurrent access.
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
                        bounded_history TEXT,
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
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS write_actions (
                        action_id TEXT PRIMARY KEY,
                        idempotency_key TEXT UNIQUE NOT NULL,
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        tool_name TEXT NOT NULL,
                        arguments TEXT NOT NULL,
                        confirmation_token TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        executed_at REAL,
                        result TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_write_actions_lookup 
                    ON write_actions (chat_id, confirmation_token, status)
                    """
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS active_cancellations (
                        chat_id INTEGER PRIMARY KEY,
                        cancelled_at REAL NOT NULL,
                        reason TEXT
                    )
                    """
                )

                # Ensure bounded_history, forward_info, and media_type columns exist for backward compatibility
                cursor = conn.execute("PRAGMA table_info(requests)")
                columns = [col["name"] for col in cursor.fetchall()]
                if "bounded_history" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN bounded_history TEXT")
                if "forward_info" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN forward_info TEXT")
                if "media_type" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN media_type TEXT")
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
                
                bounded_hist = []
                if "bounded_history" in row.keys() and row["bounded_history"]:
                    try:
                        bounded_hist = json.loads(row["bounded_history"])
                    except Exception:
                        bounded_hist = []

                fwd_info = None
                if "forward_info" in row.keys() and row["forward_info"]:
                    try:
                        fwd_info = json.loads(row["forward_info"])
                    except Exception:
                        fwd_info = None

                m_type = row["media_type"] if "media_type" in row.keys() else None

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
                    bounded_history=bounded_hist,
                    forward_info=fwd_info,
                    media_type=m_type,
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

                bounded_hist = []
                if "bounded_history" in row.keys() and row["bounded_history"]:
                    try:
                        bounded_hist = json.loads(row["bounded_history"])
                    except Exception:
                        bounded_hist = []

                fwd_info = None
                if "forward_info" in row.keys() and row["forward_info"]:
                    try:
                        fwd_info = json.loads(row["forward_info"])
                    except Exception:
                        fwd_info = None

                m_type = row["media_type"] if "media_type" in row.keys() else None

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
                    bounded_history=bounded_hist,
                    forward_info=fwd_info,
                    media_type=m_type,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            finally:
                conn.close()

    async def save_request(self, record: RequestRecord) -> None:
        async with self._lock:
            record.updated_at = time.time()
            history_json = (
                json.dumps(record.bounded_history) if record.bounded_history else None
            )
            fwd_json = (
                json.dumps(record.forward_info) if record.forward_info else None
            )
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO requests (
                            update_id, request_id, user_id, chat_id, message_id,
                            text, status, answer, error_code, tool_calls_count,
                            bounded_history, forward_info, media_type, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(update_id) DO UPDATE SET
                            status = excluded.status,
                            answer = excluded.answer,
                            error_code = excluded.error_code,
                            tool_calls_count = excluded.tool_calls_count,
                            bounded_history = excluded.bounded_history,
                            forward_info = excluded.forward_info,
                            media_type = excluded.media_type,
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
                            history_json,
                            fwd_json,
                            record.media_type,
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

    # ==========================================
    # Write Actions Persistence & Idempotency
    # ==========================================

    async def save_write_action(self, action: ActionRecord) -> None:
        async with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO write_actions (
                            action_id, idempotency_key, chat_id, user_id,
                            tool_name, arguments, confirmation_token, status,
                            created_at, expires_at, executed_at, result
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(action_id) DO UPDATE SET
                            status = excluded.status,
                            executed_at = excluded.executed_at,
                            result = excluded.result
                        """,
                        (
                            action.action_id,
                            action.idempotency_key,
                            action.chat_id,
                            action.user_id,
                            action.tool_name,
                            json.dumps(action.arguments),
                            action.confirmation_token,
                            action.status,
                            action.created_at,
                            action.expires_at,
                            action.executed_at,
                            action.result,
                        ),
                    )
            finally:
                conn.close()

    async def get_write_action(self, action_id: str) -> Optional[ActionRecord]:
        async with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT * FROM write_actions WHERE action_id = ?", (action_id,)
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return ActionRecord(
                    action_id=row["action_id"],
                    idempotency_key=row["idempotency_key"],
                    chat_id=row["chat_id"],
                    user_id=row["user_id"],
                    tool_name=row["tool_name"],
                    arguments=json.loads(row["arguments"]),
                    confirmation_token=row["confirmation_token"],
                    status=row["status"],
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                    executed_at=row["executed_at"],
                    result=row["result"],
                )
            finally:
                conn.close()

    async def get_action_by_token_or_id(
        self, chat_id: int, token_or_id: str
    ) -> Optional[ActionRecord]:
        async with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """
                    SELECT * FROM write_actions 
                    WHERE chat_id = ? AND (confirmation_token = ? OR action_id = ?)
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (chat_id, token_or_id, token_or_id),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return ActionRecord(
                    action_id=row["action_id"],
                    idempotency_key=row["idempotency_key"],
                    chat_id=row["chat_id"],
                    user_id=row["user_id"],
                    tool_name=row["tool_name"],
                    arguments=json.loads(row["arguments"]),
                    confirmation_token=row["confirmation_token"],
                    status=row["status"],
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                    executed_at=row["executed_at"],
                    result=row["result"],
                )
            finally:
                conn.close()

    async def claim_action_for_execution(
        self, action_id: str, user_id: int
    ) -> bool:
        """
        Atomically transition action from 'pending' to 'in_progress',
        verifying unused state, authorized sender, and non-expiry.
        Guarantees idempotency and at-most-once execution.
        """
        async with self._lock:
            conn = self._get_connection()
            try:
                now = time.time()
                with conn:
                    cursor = conn.execute(
                        """
                        UPDATE write_actions
                        SET status = 'in_progress'
                        WHERE action_id = ? 
                          AND user_id = ? 
                          AND status = 'pending' 
                          AND expires_at > ?
                        """,
                        (action_id, user_id, now),
                    )
                    return cursor.rowcount == 1
            finally:
                conn.close()

    async def complete_action_execution(
        self, action_id: str, result_text: str, success: bool = True
    ) -> None:
        async with self._lock:
            conn = self._get_connection()
            try:
                now = time.time()
                status = "executed" if success else "failed"
                with conn:
                    conn.execute(
                        """
                        UPDATE write_actions
                        SET status = ?, executed_at = ?, result = ?
                        WHERE action_id = ?
                        """,
                        (status, now, result_text, action_id),
                    )
            finally:
                conn.close()

    async def set_chat_cancelled(
        self, chat_id: int, reason: str = "User cancelled via /cancel"
    ) -> None:
        async with self._lock:
            conn = self._get_connection()
            try:
                now = time.time()
                with conn:
                    conn.execute(
                        """
                        INSERT INTO active_cancellations (chat_id, cancelled_at, reason)
                        VALUES (?, ?, ?)
                        ON CONFLICT(chat_id) DO UPDATE SET
                            cancelled_at = excluded.cancelled_at,
                            reason = excluded.reason
                        """,
                        (chat_id, now, reason),
                    )
            finally:
                conn.close()

    async def is_chat_cancelled(self, chat_id: int) -> bool:
        async with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT 1 FROM active_cancellations WHERE chat_id = ?",
                    (chat_id,),
                )
                return cursor.fetchone() is not None
            finally:
                conn.close()

    async def clear_chat_cancellation(self, chat_id: int) -> None:
        async with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        "DELETE FROM active_cancellations WHERE chat_id = ?",
                        (chat_id,),
                    )
            finally:
                conn.close()

    async def get_executed_actions_for_chat(self, chat_id: int) -> List[ActionRecord]:
        async with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """
                    SELECT * FROM write_actions
                    WHERE chat_id = ? AND status = 'executed'
                    ORDER BY executed_at ASC
                    """,
                    (chat_id,),
                )
                records = []
                for row in cursor.fetchall():
                    records.append(
                        ActionRecord(
                            action_id=row["action_id"],
                            idempotency_key=row["idempotency_key"],
                            chat_id=row["chat_id"],
                            user_id=row["user_id"],
                            tool_name=row["tool_name"],
                            arguments=json.loads(row["arguments"]),
                            confirmation_token=row["confirmation_token"],
                            status=row["status"],
                            created_at=row["created_at"],
                            expires_at=row["expires_at"],
                            executed_at=row["executed_at"],
                            result=row["result"],
                        )
                    )
                return records
            finally:
                conn.close()


# Singleton storage instance
storage = Storage()
