"""AI chat-command endpoints (Anthropic key management + the command relay).

Mounted on the device router (so every endpoint inherits device JWT auth) with
the ``/api/ai`` prefix:

* ``GET    /api/ai/key``        — is a key configured, and from where (never the key).
* ``PUT    /api/ai/key``        — store a user-supplied Anthropic API key (0600 file).
* ``DELETE /api/ai/key``        — remove the stored key.
* ``POST   /api/ai/command``    — run one chat command through the Claude tool loop.
* ``POST   /api/ai/transcribe`` — transcribe one uploaded voice clip to text.

The heavy lifting lives in :mod:`nomothetic.ai_command` and
:mod:`nomothetic.stt`; this module only maps HTTP shapes onto them.  The
command and transcribe endpoints are rate limited (``ai_limiter`` /
``stt_limiter`` on ``app.state``) because commands fan out into several
provider requests and transcription is CPU-heavy on the Pi.
"""

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from nomothetic.ai_command import (
    AiCommandService,
    AiKeyStore,
    AiKeyStoreError,
    AiProviderAuthError,
    AiProviderError,
    AiUnavailableError,
    validate_api_key_format,
)
from nomothetic.ai_conversations import (
    AiConversationStore,
    ConversationBusyError,
    ConversationNotFoundError,
)
from nomothetic.rate_limit import ai_rate_limit, stt_rate_limit
from nomothetic.stt import SttEngine, SttTranscriptionError, SttUnavailableError

logger = logging.getLogger(__name__)

_DEFAULT_MAX_AUDIO_BYTES = 2_000_000


def _max_audio_bytes() -> int:
    """Return the transcription upload cap in bytes (``NOMON_STT_MAX_BYTES``)."""
    try:
        return int(os.environ.get("NOMON_STT_MAX_BYTES", str(_DEFAULT_MAX_AUDIO_BYTES)))
    except ValueError:
        return _DEFAULT_MAX_AUDIO_BYTES


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AiCommandRequest(BaseModel):
    """A chat command: the new user message, continuing a conversation if given.

    Prior turns are never accepted from the client (review S-24) — the device
    holds each conversation's history and issues its ``conversation_id``.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1, max_length=8000)
    conversation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Id from an earlier response; omit to start a new conversation.",
    )


class AiActionRecord(BaseModel):
    """One tool call the model made while handling the command."""

    tool: str
    input: dict[str, Any]
    ok: bool
    result: Any = None
    error: str | None = None


class AiCommandResponse(BaseModel):
    """The model's reply plus a record of every robot action it took."""

    reply: str
    actions: list[AiActionRecord]
    model: str
    stop_reason: str
    conversation_id: str
    timestamp: str


class AiKeySetRequest(BaseModel):
    """Payload to store a user-supplied Anthropic API key."""

    api_key: str = Field(..., min_length=1, max_length=512)


class AiKeyStatusResponse(BaseModel):
    """Whether an AI key is configured and where it comes from — never the key itself."""

    configured: bool
    source: str | None = Field(default=None, description='"stored", "env", or null')
    timestamp: str


class TranscribeResponse(BaseModel):
    """The transcript of one uploaded voice clip (empty text means silence)."""

    text: str
    engine: str
    timestamp: str


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_ai_router() -> APIRouter:
    """Build the AI command router.

    The :class:`~nomothetic.ai_command.AiCommandService` and
    :class:`~nomothetic.ai_command.AiKeyStore` are read from ``app.state`` at
    call time so test fixtures can inject fakes.

    Returns
    -------
    APIRouter
        Router with ``/api/ai/key`` (GET/PUT/DELETE) and ``/api/ai/command``.
    """
    router = APIRouter(prefix="/api/ai", tags=["AI"])

    def _key_store(request: Request) -> AiKeyStore:
        store: AiKeyStore | None = getattr(request.app.state, "ai_key_store", None)
        if store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI key store not configured",
            )
        return store

    def _service(request: Request) -> AiCommandService:
        service: AiCommandService | None = getattr(request.app.state, "ai_service", None)
        if service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI command service not configured",
            )
        return service

    def _conversations(request: Request) -> AiConversationStore:
        store: AiConversationStore | None = getattr(request.app.state, "ai_conversations", None)
        if store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI conversation store not configured",
            )
        return store

    def _stt_engine(request: Request) -> SttEngine:
        engine: SttEngine | None = getattr(request.app.state, "stt_engine", None)
        if engine is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Voice transcription is not enabled on this device",
            )
        return engine

    def _key_status(store: AiKeyStore) -> AiKeyStatusResponse:
        source = store.source()
        return AiKeyStatusResponse(
            configured=source is not None,
            source=source,
            timestamp=_now(),
        )

    @router.get("/key", response_model=AiKeyStatusResponse)
    async def get_key_status(request: Request):
        """Report whether an Anthropic API key is configured (and its source)."""
        store = _key_store(request)
        return await asyncio.to_thread(_key_status, store)

    @router.put("/key", response_model=AiKeyStatusResponse)
    async def set_key(body: AiKeySetRequest, request: Request):
        """Store a user-supplied Anthropic API key in the device's 0600 key file.

        Raises
        ------
        HTTPException
            400 if the key does not look like an Anthropic API key,
            503 if it could not be persisted.
        """
        store = _key_store(request)
        try:
            key = validate_api_key_format(body.api_key)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        try:
            await asyncio.to_thread(store.save, key)
        except AiKeyStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        return await asyncio.to_thread(_key_status, store)

    @router.delete("/key", response_model=AiKeyStatusResponse)
    async def clear_key(request: Request):
        """Remove the stored API key (the env fallback, if any, remains).

        Raises
        ------
        HTTPException
            503 if the key file exists but could not be removed.
        """
        store = _key_store(request)
        try:
            await asyncio.to_thread(store.clear)
        except AiKeyStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        return await asyncio.to_thread(_key_status, store)

    @router.post(
        "/command",
        response_model=AiCommandResponse,
        dependencies=[Depends(ai_rate_limit)],
    )
    async def run_ai_command(body: AiCommandRequest, request: Request):
        """Run one chat command through the Claude robot-tool loop.

        The conversation history comes from the device's own store, never the
        request; the response carries the ``conversation_id`` to continue it.

        Raises
        ------
        HTTPException
            404 if ``conversation_id`` is unknown or expired (start a new one),
            409 if a command is already running in that conversation,
            503 if no API key is configured or AI support is not installed,
            502 if the AI provider rejects the key or the request fails,
            429 when rate limited.
        """
        service = _service(request)
        store = _key_store(request)
        conversations = _conversations(request)
        resolved = await asyncio.to_thread(store.resolve)
        if resolved is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No AI API key configured. Store one via PUT /api/ai/key.",
            )
        api_key, _source = resolved
        try:
            conversation_id, history = conversations.begin(body.conversation_id, body.message)
        except ConversationNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found or expired; start a new one.",
            ) from exc
        except ConversationBusyError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A command is already running in this conversation.",
            ) from exc
        try:
            result = await service.run_command(history, api_key=api_key)
        except AiUnavailableError as exc:
            conversations.abort(conversation_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        except (AiProviderAuthError, AiProviderError) as exc:
            conversations.abort(conversation_id)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        except BaseException:
            # Cancellation (client disconnect) or an unexpected failure must not
            # leave the conversation stuck busy.
            conversations.abort(conversation_id)
            raise
        conversations.commit(conversation_id, body.message, result["reply"])
        logger.info(
            "AI command completed: %d action(s), stop_reason=%s",
            len(result["actions"]),
            result["stop_reason"],
        )
        return AiCommandResponse(timestamp=_now(), conversation_id=conversation_id, **result)

    @router.post(
        "/transcribe",
        response_model=TranscribeResponse,
        dependencies=[Depends(stt_rate_limit)],
    )
    async def transcribe_audio(request: Request, audio: UploadFile = File(...)):
        """Transcribe one uploaded voice clip to text.

        Accepts any common container/codec (m4a, webm, wav, ...); the engine
        normalises it on-device.  An empty ``text`` means the clip contained
        no recognisable speech — that is a successful response, not an error.

        Raises
        ------
        HTTPException
            503 if transcription is not enabled or its prerequisites (vosk,
            model, ffmpeg) are missing on the device,
            413 if the upload exceeds ``NOMON_STT_MAX_BYTES``,
            422 if the upload is empty or undecodable,
            429 when rate limited.
        """
        engine = _stt_engine(request)
        max_bytes = _max_audio_bytes()
        data = await audio.read()
        if len(data) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"Audio upload exceeds the {max_bytes} byte limit",
            )
        if len(data) == 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Audio upload is empty",
            )
        content_type = audio.content_type or "application/octet-stream"
        try:
            result = await asyncio.to_thread(engine.transcribe, data, content_type)
        except SttUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        except SttTranscriptionError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        return TranscribeResponse(text=result.text, engine=result.engine, timestamp=_now())

    return router
