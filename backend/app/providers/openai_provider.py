import os
from typing import List, Dict, Any, Optional
from backend.app.providers.base import BaseModelProvider, ModelResponse, ToolCall


class OpenAIProvider(BaseModelProvider):
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o")

    async def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
    ) -> ModelResponse:
        # Stub implementation for initial project structure
        # To be filled in Task 07 (Milestone 2)
        if not self.api_key:
            return ModelResponse(text="[OpenAI Stub] Assistant response")

        return ModelResponse(text="[OpenAI Stub] Assistant response")
