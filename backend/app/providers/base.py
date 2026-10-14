from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from pydantic import BaseModel


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: Dict[str, Any]


class ModelResponse(BaseModel):
    text: Optional[str] = None
    tool_calls: List[ToolCall] = []
    stop_reason: Optional[str] = None


class BaseModelProvider(ABC):
    """
    Abstract adapter for LLM providers (Anthropic, OpenAI).
    Preserves provider tool-call IDs and formats tool schemas.
    """

    @abstractmethod
    async def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
    ) -> ModelResponse:
        """
        Generate completion or tool call from model.
        """
        pass
