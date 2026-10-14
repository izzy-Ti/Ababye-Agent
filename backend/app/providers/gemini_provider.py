import os
import logging
from typing import List, Dict, Any, Optional
from backend.app.providers.base import BaseModelProvider, ModelResponse, ToolCall

logger = logging.getLogger(__name__)


class GeminiProvider(BaseModelProvider):
    """
    Google Gemini model provider adapter using the google-genai SDK.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    async def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
    ) -> ModelResponse:
        if not self.api_key:
            logger.warning("GEMINI_API_KEY not configured, returning fallback response")
            return ModelResponse(text="[Gemini Stub] Assistant response")

        try:
            from google import genai

            client = genai.Client(api_key=self.api_key)

            # Build prompt contents from history
            contents = []
            for msg in messages:
                role = msg.get("role", "user")
                # Gemini roles: "user" or "model"
                gemini_role = "model" if role == "assistant" else "user"
                contents.append({"role": gemini_role, "parts": [{"text": msg.get("content", "")}]})

            # Prepare config options
            config_kwargs: Dict[str, Any] = {}
            if system_prompt:
                config_kwargs["system_instruction"] = system_prompt

            # Asynchronous call or sync call in thread
            import asyncio

            response = await asyncio.to_thread(
                client.models.generate_content,
                model=self.model,
                contents=contents,
                config=config_kwargs if config_kwargs else None,
            )

            text_output = response.text or ""
            return ModelResponse(text=text_output, tool_calls=[])

        except Exception as e:
            logger.exception("Error calling Gemini API")
            raise e
