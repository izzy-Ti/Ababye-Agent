import os
import pytest
from unittest.mock import AsyncMock, MagicMock
from google.genai import types

from backend.app.providers.gemini_provider import (
    GeminiProvider,
    clean_json_schema_for_gemini,
    convert_mcp_to_gemini_tools,
    convert_messages_to_gemini,
)
from backend.app.mcp_manager import mcp_manager





def test_clean_json_schema_for_gemini():
    raw_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "EchoArguments",
        "type": "object",
        "properties": {
            "message": {
                "title": "Message",
                "type": "string",
                "description": "Text to echo",
            }
        },
        "required": ["message"],
    }
    cleaned = clean_json_schema_for_gemini(raw_schema)
    assert "$schema" not in cleaned
    assert "title" not in cleaned
    assert "message" in cleaned["properties"]
    assert "title" not in cleaned["properties"]["message"]
    assert cleaned["properties"]["message"]["type"] == "string"


def test_convert_mcp_to_gemini_tools():
    tools = [
        {
            "name": "echo_echo",
            "description": "Echoes message back",
            "input_schema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        }
    ]
    gemini_tools = convert_mcp_to_gemini_tools(tools)
    assert gemini_tools is not None
    assert len(gemini_tools) == 1
    assert len(gemini_tools[0].function_declarations) == 1
    decl = gemini_tools[0].function_declarations[0]
    assert decl.name == "echo_echo"
    assert decl.description == "Echoes message back"


def test_convert_messages_to_gemini():
    messages = [
        {"role": "user", "content": "Add 5 and 3"},
        {
            "role": "assistant",
            "content": "Let me calculate that.",
            "tool_calls": [
                {"id": "call_1", "name": "echo_add", "arguments": {"a": 5, "b": 3}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "echo_add", "content": "8"},
    ]
    contents = convert_messages_to_gemini(messages)
    assert len(contents) == 3
    assert contents[0].role == "user"
    assert contents[1].role == "model"
    assert contents[2].role == "user"

    # Verify tool call part in model content
    model_parts = contents[1].parts
    assert len(model_parts) == 2
    assert model_parts[0].text == "Let me calculate that."
    assert model_parts[1].function_call.name == "echo_add"
    assert model_parts[1].function_call.args == {"a": 5, "b": 3}
    assert model_parts[1].function_call.id == "call_1"

    # Verify function response in user content
    tool_parts = contents[2].parts
    assert len(tool_parts) == 1
    assert tool_parts[0].function_response.name == "echo_add"
    assert tool_parts[0].function_response.id == "call_1"
    assert tool_parts[0].function_response.response == {"output": "8"}


@pytest.mark.asyncio
async def test_gemini_provider_fallback_without_api_key():
    provider = GeminiProvider(api_key="")
    response = await provider.generate_response(
        messages=[{"role": "user", "content": "Hello"}]
    )
    assert response.text is not None
    assert "[Gemini Stub]" in response.text


@pytest.mark.asyncio
async def test_fake_model_tool_flow_with_mcp():
    """
    Simulates a complete fake model turn:
    1. Turn 1: Model requests 'echo_add' with arguments a=15, b=27.
    2. MCP execution: Real MCP echo server executes the tool and produces 42.
    3. Turn 2: Tool result is fed back into model history.
    4. Model produces final text: '15 + 27 = 42'.
    """
    # 1. Initialize real MCP server
    await mcp_manager.initialize()
    available_tools = await mcp_manager.get_tools()
    tools_dict = [t.to_dict() for t in available_tools]

    # Create mock fake client for Gemini SDK
    fake_client = MagicMock()
    fake_client.aio = MagicMock()
    fake_client.aio.models = MagicMock()

    # Turn 1 response: function call
    turn1_candidate = types.Candidate(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="echo_add",
                        args={"a": 15, "b": 27},
                        id="call_test_add_42",
                    )
                )
            ],
        )
    )
    turn1_response = types.GenerateContentResponse(candidates=[turn1_candidate])

    # Turn 2 response: final text after receiving tool result
    turn2_candidate = types.Candidate(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="The sum of 15 and 27 is 42.")],
        )
    )
    turn2_response = types.GenerateContentResponse(candidates=[turn2_candidate])

    fake_client.aio.models.generate_content = AsyncMock(
        side_effect=[turn1_response, turn2_response]
    )

    provider = GeminiProvider(api_key="fake_key", client=fake_client)

    try:
        # Turn 1: Model requests tool
        history = [{"role": "user", "content": "Compute 15 + 27"}]
        res1 = await provider.generate_response(
            messages=history,
            tools=tools_dict,
            system_prompt="You are a helpful assistant.",
        )

        assert res1.stop_reason == "tool_use"
        assert len(res1.tool_calls) == 1
        call = res1.tool_calls[0]
        assert call.id == "call_test_add_42"
        assert call.name == "echo_add"
        assert call.arguments == {"a": 15, "b": 27}

        # Execute tool via real MCP server
        tool_result = await mcp_manager.execute_tool(call.name, call.arguments)
        assert tool_result["text"] == "42"

        # Append model turn and tool result to conversation history
        history.append({
            "role": "assistant",
            "content": res1.text,
            "tool_calls": [call.model_dump()],
        })
        history.append({
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": tool_result["text"],
        })

        # Turn 2: Provider feeds tool result back into model
        res2 = await provider.generate_response(
            messages=history,
            tools=tools_dict,
            system_prompt="You are a helpful assistant.",
        )

        assert res2.stop_reason == "stop"
        assert len(res2.tool_calls) == 0
        assert res2.text == "The sum of 15 and 27 is 42."

        # Verify generate_content was called twice
        assert fake_client.aio.models.generate_content.call_count == 2
    finally:
        await mcp_manager.shutdown()


@pytest.mark.asyncio
async def test_live_smoke_run():
    """
    Live smoke run with actual Gemini API if GEMINI_API_KEY is provided in the environment.
    Skipped if no real key is configured.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or "AIzaSy_your" in api_key:
        pytest.skip("GEMINI_API_KEY not configured for live smoke run")

    await mcp_manager.initialize()
    try:
        tools = await mcp_manager.get_tools()
        tools_dict = [t.to_dict() for t in tools]

        provider = GeminiProvider(api_key=api_key)

        # 1. Request tool
        history = [{"role": "user", "content": "Use the echo_add tool to compute 20 + 22. Respond only with the tool call."}]
        res1 = await provider.generate_response(
            messages=history,
            tools=tools_dict,
            system_prompt="You are an assistant that calls tools when asked.",
        )

        assert len(res1.tool_calls) > 0
        call = res1.tool_calls[0]
        assert "add" in call.name

        # 2. Execute tool
        result = await mcp_manager.execute_tool(call.name, call.arguments)
        assert "42" in result["text"]

        # 3. Supply tool response to model
        history.append({
            "role": "assistant",
            "content": res1.text,
            "tool_calls": [call.model_dump()],
        })
        history.append({
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": result["text"],
        })

        res2 = await provider.generate_response(
            messages=history,
            tools=tools_dict,
        )
        assert res2.text is not None
        assert "42" in res2.text
    finally:
        await mcp_manager.shutdown()

