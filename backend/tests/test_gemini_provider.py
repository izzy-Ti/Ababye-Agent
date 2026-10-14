import pytest
from backend.app.providers.gemini_provider import GeminiProvider


@pytest.mark.asyncio
async def test_gemini_provider_fallback_without_api_key():
    provider = GeminiProvider(api_key="")
    response = await provider.generate_response(
        messages=[{"role": "user", "content": "Hello"}]
    )
    assert response.text is not None
    assert "[Gemini Stub]" in response.text
