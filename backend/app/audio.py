import os
import logging
from typing import Optional, Dict, Any, Callable, Awaitable
import httpx

from backend.app.schemas import VoiceMetadata

logger = logging.getLogger(__name__)

# Suppress httpx and httpcore request logging to prevent leaking tokens or download URLs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class VoiceProcessingError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class AudioService:
    """
    Handles downloading voice notes from Telegram and transcribing them.
    1. Enforces size & type limits.
    2. Keeps download URLs and bot tokens out of logs (strict redaction).
    3. Transcribes speech to text via Gemini multimodal audio capabilities.
    """

    MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB Telegram download limit
    SUPPORTED_MIME_TYPES = {
        "audio/ogg",
        "audio/oga",
        "audio/opus",
        "audio/mp3",
        "audio/wav",
        "audio/m4a",
        "audio/x-m4a",
    }

    def __init__(
        self,
        bot_token: Optional[str] = None,
        transcriber_fn: Optional[Callable[[bytes, str], Awaitable[str]]] = None,
    ):
        self._bot_token = bot_token
        self._transcriber_fn = transcriber_fn

    def _get_bot_token(self) -> str:
        return self._bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")

    async def fetch_audio_bytes(self, voice_meta: VoiceMetadata) -> bytes:
        # 1. Enforce MIME type limit
        mime_type = (voice_meta.mime_type or "audio/ogg").lower()
        if mime_type not in self.SUPPORTED_MIME_TYPES:
            logger.warning(f"Rejected unsupported voice MIME type: {mime_type}")
            raise VoiceProcessingError(
                "VOICE_UNSUPPORTED_TYPE",
                f"Unsupported voice audio format: '{mime_type}'. Supported formats: {sorted(list(self.SUPPORTED_MIME_TYPES))}",
            )

        # 2. Enforce reported file size limit
        if voice_meta.file_size and voice_meta.file_size > self.MAX_FILE_SIZE_BYTES:
            logger.warning(
                f"Voice file exceeds size limit: {voice_meta.file_size} > {self.MAX_FILE_SIZE_BYTES}"
            )
            raise VoiceProcessingError(
                "VOICE_FILE_TOO_LARGE",
                f"Voice note exceeds the maximum size limit of {self.MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
            )

        # 3. Fetch from Telegram
        token = self._get_bot_token()
        if not token:
            logger.error("TELEGRAM_BOT_TOKEN not configured on backend")
            raise VoiceProcessingError(
                "VOICE_DOWNLOAD_FAILED",
                "Backend is not configured with a Telegram Bot token to download voice notes.",
            )

        # Get file metadata from Telegram (logs redact token)
        logger.info(f"Fetching metadata for voice file_id='{voice_meta.file_id}' [TOKEN REDACTED]")
        get_file_url = f"https://api.telegram.org/bot{token}/getFile"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(get_file_url, params={"file_id": voice_meta.file_id})
                if resp.status_code != 200:
                    logger.warning(f"Telegram getFile returned HTTP {resp.status_code} [TOKEN REDACTED]")
                    raise VoiceProcessingError(
                        "VOICE_DOWNLOAD_FAILED",
                        f"Failed to retrieve voice file info from Telegram (HTTP {resp.status_code}).",
                    )

                data = resp.json()
                if not data.get("ok") or not data.get("result", {}).get("file_path"):
                    err_desc = data.get("description", "Unknown Telegram error")
                    logger.warning(f"Telegram getFile error: {err_desc} [TOKEN REDACTED]")
                    raise VoiceProcessingError(
                        "VOICE_DOWNLOAD_FAILED",
                        f"Telegram rejected voice download: {err_desc}",
                    )

                file_path = data["result"]["file_path"]
                file_size = data["result"].get("file_size")

                if file_size and file_size > self.MAX_FILE_SIZE_BYTES:
                    logger.warning(f"Remote file size {file_size} exceeds limit")
                    raise VoiceProcessingError(
                        "VOICE_FILE_TOO_LARGE",
                        f"Voice note exceeds the maximum size limit of {self.MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
                    )

                # Download raw audio bytes (logs strictly redact download URL and token)
                logger.info("Downloading audio bytes from Telegram Bot API [URL REDACTED]")
                download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                audio_resp = await client.get(download_url)

                if audio_resp.status_code != 200:
                    logger.warning(
                        f"Telegram file download returned HTTP {audio_resp.status_code} [URL REDACTED]"
                    )
                    raise VoiceProcessingError(
                        "VOICE_DOWNLOAD_FAILED",
                        f"Failed to download audio content from Telegram (HTTP {audio_resp.status_code}).",
                    )

                audio_bytes = audio_resp.content
                if len(audio_bytes) > self.MAX_FILE_SIZE_BYTES:
                    logger.warning(f"Downloaded audio length {len(audio_bytes)} exceeds limit")
                    raise VoiceProcessingError(
                        "VOICE_FILE_TOO_LARGE",
                        f"Voice note exceeds the maximum size limit of {self.MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
                    )

                return audio_bytes

        except VoiceProcessingError:
            raise
        except Exception as e:
            logger.warning(f"Network error downloading voice note: {type(e).__name__} [URL REDACTED]")
            raise VoiceProcessingError(
                "VOICE_DOWNLOAD_FAILED",
                f"Network failure while downloading voice note: {type(e).__name__}",
            )

    async def transcribe(self, audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
        """
        Transcribe raw audio bytes using Gemini multimodal audio model.
        """
        if self._transcriber_fn is not None:
            return await self._transcriber_fn(audio_bytes, mime_type)

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY not configured for voice transcription, returning stub")
            return "[Transcribed voice note placeholder]"

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            model_id = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

            contents = [
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                types.Part.from_text(
                    text="Transcribe this voice note verbatim. Respond only with the exact transcribed text, without any commentary or quotation marks."
                ),
            ]

            response = await client.aio.models.generate_content(
                model=model_id,
                contents=contents,
            )

            transcript = (response.text or "").strip()
            if not transcript:
                raise VoiceProcessingError(
                    "VOICE_TRANSCRIPTION_FAILED",
                    "Speech transcription returned empty text from audio model.",
                )

            return transcript

        except VoiceProcessingError:
            raise
        except Exception as e:
            logger.exception("Error during audio transcription with Gemini")
            raise VoiceProcessingError(
                "VOICE_TRANSCRIPTION_FAILED",
                f"Failed to transcribe voice note: {str(e)}",
            )

    async def process_voice_note(self, voice_meta: VoiceMetadata) -> str:
        """
        Full pipeline: fetch bytes with safety checks, then transcribe.
        """
        audio_bytes = await self.fetch_audio_bytes(voice_meta)
        mime = voice_meta.mime_type or "audio/ogg"
        return await self.transcribe(audio_bytes, mime)


# Global default audio service
audio_service = AudioService()
