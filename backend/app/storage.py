import os
import time
from typing import Optional, Dict, Any, List


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
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Storage:
    """
    Storage layer for request deduplication, state persistence, and chat context.
    """

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or os.environ.get("SHARED_STORAGE_URL", "memory://")
        # In-memory storage cache / fallback
        self._requests_by_update_id: Dict[int, RequestRecord] = {}
        self._requests_by_id: Dict[str, RequestRecord] = {}
        self._chat_history: Dict[int, List[Dict[str, str]]] = {}

    async def get_by_update_id(self, update_id: int) -> Optional[RequestRecord]:
        return self._requests_by_update_id.get(update_id)

    async def get_by_request_id(self, request_id: str) -> Optional[RequestRecord]:
        return self._requests_by_id.get(request_id)

    async def save_request(self, record: RequestRecord) -> None:
        record.updated_at = time.time()
        self._requests_by_update_id[record.update_id] = record
        self._requests_by_id[record.request_id] = record

    async def get_chat_history(self, chat_id: int, limit: int = 10) -> List[Dict[str, str]]:
        history = self._chat_history.get(chat_id, [])
        return history[-limit:]

    async def append_chat_message(self, chat_id: int, role: str, content: str) -> None:
        if chat_id not in self._chat_history:
            self._chat_history[chat_id] = []
        self._chat_history[chat_id].append({"role": role, "content": content})

    async def clear_chat_history(self, chat_id: int) -> None:
        self._chat_history[chat_id] = []


# Singleton storage instance
storage = Storage()
