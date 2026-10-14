import os
import logging
from typing import List, Dict, Any, Optional
from google import genai
from google.genai import types

from backend.app.providers.base import BaseModelProvider, ModelResponse, ToolCall

logger = logging.getLogger(__name__)


def clean_json_schema_for_gemini(schema: Any) -> Any:
    """
    Clean an MCP JSON schema to be strictly compatible with Gemini function declarations:
    - Removes top-level schema metadata ($schema, title) that might confuse the parser.
    - Recursively processes properties and array items.
    - Ensures top-level schema has type="object".
    """
    if not isinstance(schema, dict):
        return schema

    cleaned: Dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("$schema", "title"):
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {
                prop_name: clean_json_schema_for_gemini(prop_val)
                for prop_name, prop_val in value.items()
            }
        elif key == "items" and isinstance(value, dict):
            cleaned[key] = clean_json_schema_for_gemini(value)
        else:
            cleaned[key] = value

    if "type" not in cleaned:
        cleaned["type"] = "object"

    return cleaned


def convert_mcp_to_gemini_tools(
    tools: Optional[List[Any]],
) -> Optional[List[types.Tool]]:
    """
    Convert a list of MCP tool definitions or dicts into Gemini types.Tool objects.
    Each MCP tool specifies name, description, and input_schema.
    """
    if not tools:
        return None

    declarations: List[types.FunctionDeclaration] = []
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name", "")
            description = tool.get("description", "")
            schema = tool.get("input_schema") or tool.get("inputSchema") or tool.get("parameters") or {}
        else:
            name = getattr(tool, "name", "")
            description = getattr(tool, "description", "")
            schema = getattr(tool, "input_schema", {})

        cleaned_schema = clean_json_schema_for_gemini(schema)
        declaration = types.FunctionDeclaration(
            name=name,
            description=description,
            parameters=cleaned_schema,
        )
        declarations.append(declaration)

    return [types.Tool(function_declarations=declarations)]


def convert_messages_to_gemini(
    messages: List[Dict[str, Any]],
) -> List[types.Content]:
    """
    Convert provider-agnostic conversation turns into Gemini types.Content objects.
    Handles user text, model text, model function calls, and user function responses.
    """
    contents: List[types.Content] = []

    for msg in messages:
        role = msg.get("role", "user")
        parts: List[types.Part] = []

        if role == "user":
            content = msg.get("content", "")
            if content:
                parts.append(types.Part.from_text(text=str(content)))
            if parts:
                contents.append(types.Content(role="user", parts=parts))

        elif role in ("assistant", "model"):
            content = msg.get("content")
            if content:
                parts.append(types.Part.from_text(text=str(content)))

            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                tc_name = tc.get("name") if isinstance(tc, dict) else tc.name
                tc_args = tc.get("arguments", {}) if isinstance(tc, dict) else tc.arguments
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            name=tc_name,
                            args=tc_args or {},
                            id=tc_id,
                        )
                    )
                )

            if parts:
                contents.append(types.Content(role="model", parts=parts))

        elif role in ("tool", "tool_result"):
            tool_name = msg.get("name", "")
            tool_call_id = msg.get("tool_call_id")
            raw_content = msg.get("content", "")

            if isinstance(raw_content, dict):
                response_dict = raw_content
            else:
                response_dict = {"output": raw_content}

            # Function responses in Gemini are submitted under the 'user' role
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=tool_name,
                        response=response_dict,
                        id=tool_call_id,
                    )
                )
            )
            contents.append(types.Content(role="user", parts=parts))

    return contents


class GeminiProvider(BaseModelProvider):
    """
    Google Gemini model provider adapter using the official google-genai SDK.
    Translates MCP tool schemas to Gemini FunctionDeclarations, preserves
    provider tool-call IDs, and parses responses into normalized ModelResponse objects.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        client: Optional[Any] = None,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key:
            return None
        return genai.Client(api_key=self.api_key)

    async def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
    ) -> ModelResponse:
        client = self._get_client()

        if client is None:
            logger.warning("GEMINI_API_KEY not configured, returning fallback response")
            return ModelResponse(text="[Gemini Stub] Assistant response")

        try:
            contents = convert_messages_to_gemini(messages)
            gemini_tools = convert_mcp_to_gemini_tools(tools)

            config = types.GenerateContentConfig(
                system_instruction=system_prompt if system_prompt else None,
                tools=gemini_tools if gemini_tools else None,
            )

            # Call asynchronous generate_content
            response = await client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )

            text_parts: List[str] = []
            extracted_tool_calls: List[ToolCall] = []

            if response.candidates:
                for candidate in response.candidates:
                    if not candidate.content or not candidate.content.parts:
                        continue
                    for idx, part in enumerate(candidate.content.parts):
                        if part.text:
                            text_parts.append(part.text)
                        if part.function_call:
                            fc = part.function_call
                            call_id = getattr(fc, "id", None) or f"call_{idx}_{fc.name}"
                            extracted_tool_calls.append(
                                ToolCall(
                                    id=call_id,
                                    name=fc.name,
                                    arguments=fc.args or {},
                                )
                            )

            final_text = "".join(text_parts).strip() if text_parts else None
            stop_reason = "tool_use" if extracted_tool_calls else "stop"

            return ModelResponse(
                text=final_text,
                tool_calls=extracted_tool_calls,
                stop_reason=stop_reason,
            )

        except Exception as e:
            logger.exception("Error calling Gemini API")
            raise e
