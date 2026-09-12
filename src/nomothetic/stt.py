"""Speech-to-text engines for voice commands.

This module powers ``POST /api/ai/transcribe`` (see :mod:`nomothetic.ai_routes`).
The operator's app records a short audio clip (m4a/AAC on mobile, webm/Opus on
web) and uploads it; the device transcribes it locally and hands the text to the
same chat-command path the keyboard uses.  Like the AI relay itself (Phase 26),
this is *operator convenience, not autonomy* — no cognition or robot state lives
here (ADR-004).  The robot's own microphone enters this picture only through the
wake-word listener (:mod:`nomothetic.wake`, ADR-021), which shares this engine's
model via :meth:`VoskSttEngine.create_recognizer`.

The engine is pluggable behind the :class:`SttEngine` protocol so a different
model or a cloud transcription service can be wired in ``create_app()`` without
touching the route.  The only built-in engine is :class:`VoskSttEngine`:

* **Offline** — the Vosk small English model runs entirely on the device.
* **Lazy loaded** — the model costs seconds and a large slice of the Pi Zero
  2W's 512 MB to load, so it is loaded on the first transcription request,
  never at API startup, and kept until :meth:`VoskSttEngine.unload` releases
  it (the wake-word listener does this when disabled via
  ``PUT /api/voice/wake``, ADR-021).
* **Serialized** — recognition holds the engine lock; concurrent uploads queue
  rather than multiplying peak memory.

Audio is normalised to 16 kHz mono PCM with an ``ffmpeg`` subprocess, so the
endpoint accepts whatever container the client records.  The ``vosk`` package
is an optional dependency (``nomothetic[stt]``); when it, the model directory,
or ``ffmpeg`` is missing this module still imports and transcription raises
:class:`SttUnavailableError` (mapped to HTTP 503).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol

try:
    import vosk
except ImportError:  # pragma: no cover - exercised by patching vosk to None
    vosk = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_MODEL_PATH = "/var/lib/nomon/stt/vosk-model-small-en-us-0.15"

_SAMPLE_RATE_HZ = 16000
_PCM_CHUNK_BYTES = 32000
_FFMPEG_TIMEOUT_S = 30.0


# ============================================================================
# Errors
# ============================================================================


class SttUnavailableError(Exception):
    """STT cannot run on this device (missing vosk, model directory, or ffmpeg)."""


class SttTranscriptionError(Exception):
    """The uploaded audio could not be decoded or recognised."""


# ============================================================================
# Protocol & result
# ============================================================================


@dataclass
class SttResult:
    """The outcome of one transcription.

    Attributes
    ----------
    text : str
        Recognised text; empty when the clip contained no recognisable speech.
    engine : str
        Name of the engine that produced the transcript (e.g. ``"vosk"``).
    """

    text: str
    engine: str


class SttEngine(Protocol):
    """A speech-to-text backend usable by the transcription endpoint."""

    def transcribe(self, audio: bytes, content_type: str) -> SttResult:
        """Transcribe one audio clip to text.

        Parameters
        ----------
        audio : bytes
            Raw uploaded audio in any common container/codec.
        content_type : str
            The upload's MIME type, for engines/diagnostics that care about it.

        Returns
        -------
        SttResult
            Recognised text (possibly empty) and the engine name.

        Raises
        ------
        SttUnavailableError
            The engine's runtime prerequisites are missing.
        SttTranscriptionError
            The audio could not be decoded or recognised.
        """
        ...  # pragma: no cover - protocol definition


# ============================================================================
# Audio normalisation
# ============================================================================


def _to_pcm(audio: bytes) -> bytes:
    """Convert arbitrary audio bytes to 16 kHz mono signed-16-bit-LE PCM.

    Uses an ``ffmpeg`` subprocess (stdin → stdout) so any container/codec the
    client records is accepted without Python-side codec dependencies.

    Parameters
    ----------
    audio : bytes
        Raw audio in any format ffmpeg can demux (m4a, webm, wav, ...).

    Returns
    -------
    bytes
        Raw PCM suitable for feeding a Vosk recogniser.

    Raises
    ------
    SttUnavailableError
        ``ffmpeg`` is not installed on the device.
    SttTranscriptionError
        ffmpeg could not decode the payload (corrupt or unsupported audio).
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SttUnavailableError("ffmpeg is not installed on the device (apt install ffmpeg)")
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-ar",
                str(_SAMPLE_RATE_HZ),
                "-ac",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ],
            input=audio,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise SttTranscriptionError("audio decode timed out") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip().splitlines()
        raise SttTranscriptionError(
            f"could not decode audio: {detail[-1] if detail else 'unknown ffmpeg error'}"
        )
    if len(proc.stdout) == 0:
        raise SttTranscriptionError("audio decoded to no samples")
    return proc.stdout


# ============================================================================
# Vosk engine
# ============================================================================


class VoskSttEngine:
    """Offline transcription with a local Vosk model.

    Parameters
    ----------
    model_path : str, optional
        Directory containing an unpacked Vosk model.  Defaults to the
        ``NOMON_STT_MODEL_PATH`` environment variable, then
        :data:`DEFAULT_MODEL_PATH`.
    """

    name = "vosk"

    def __init__(self, model_path: str | None = None) -> None:
        self._model_path = model_path or os.environ.get("NOMON_STT_MODEL_PATH", DEFAULT_MODEL_PATH)
        self._model: Any | None = None
        self._lock = Lock()

    def _load_model(self) -> Any:
        """Load the Vosk model once; callers must hold ``self._lock``.

        Raises
        ------
        SttUnavailableError
            The ``vosk`` package or the model directory is missing.
        """
        if vosk is None:
            raise SttUnavailableError(
                "STT support is not installed on the device (nomothetic[stt] extra)"
            )
        if self._model is None:
            if not os.path.isdir(self._model_path):
                raise SttUnavailableError(
                    f"STT model not installed at {self._model_path} "
                    "(run scripts/fetch_stt_model.sh on the device)"
                )
            logger.info("loading vosk model from %s", self._model_path)
            vosk.SetLogLevel(-1)
            self._model = vosk.Model(self._model_path)
            logger.info("vosk model loaded")
        return self._model

    def unload(self) -> bool:
        """Release the loaded model, freeing its memory back to the OS.

        Idempotent — a no-op returning False when nothing is loaded. Callers
        must ensure nothing still holds a recognizer built from the current
        model before calling this: ``vosk.KaldiRecognizer`` wraps a raw
        handle rather than a Python reference to the model, so freeing it out
        from under a live recognizer is a use-after-free, not a graceful
        degrade. The wake-word listener only calls this once its thread —
        and therefore its recognizers — has fully stopped (see
        :meth:`nomothetic.wake.WakeWordListener.unload_model`). The next
        call to :meth:`transcribe` or :meth:`create_recognizer` reloads the
        model from disk at the usual multi-second cost.

        Returns
        -------
        bool
            True if a model was loaded and has now been released.
        """
        with self._lock:
            if self._model is None:
                return False
            self._model = None
            logger.info("vosk model unloaded")
            return True

    def create_recognizer(
        self, sample_rate_hz: float = _SAMPLE_RATE_HZ, grammar_json: str | None = None
    ) -> Any:
        """Build a streaming ``KaldiRecognizer`` that shares this engine's model.

        The wake-word listener (ADR-021) uses this to run continuous
        recognition on the robot's microphone without loading a second copy of
        the model into the Pi Zero 2W's 512 MB of RAM.  The engine lock is
        held only while the model is loaded and the recognizer constructed, so
        a streaming recognizer never serialises against :meth:`transcribe`.

        Parameters
        ----------
        sample_rate_hz : float
            Sample rate of the PCM that will be fed to the recognizer.
        grammar_json : str | None
            Optional JSON array of accepted phrases (e.g.
            ``'["hey nomon", "[unk]"]'``) to constrain recognition to a
            keyword grammar.  ``None`` uses the model's full vocabulary.

        Returns
        -------
        Any
            A ``vosk.KaldiRecognizer``.  Recognizers are cheap, stateful, and
            single-thread-use; the underlying model is shared and thread-safe.

        Raises
        ------
        SttUnavailableError
            The ``vosk`` package or the model directory is missing.
        """
        with self._lock:
            model = self._load_model()
        if grammar_json is not None:
            return vosk.KaldiRecognizer(model, sample_rate_hz, grammar_json)
        return vosk.KaldiRecognizer(model, sample_rate_hz)

    def transcribe(self, audio: bytes, content_type: str) -> SttResult:
        """Transcribe one audio clip with the local Vosk model.

        Blocking (ffmpeg subprocess + recognition); callers on the event loop
        must wrap this in ``asyncio.to_thread``.  The engine lock serialises
        recognition so concurrent uploads queue instead of multiplying peak
        memory on the Pi.

        Parameters
        ----------
        audio : bytes
            Raw uploaded audio in any ffmpeg-decodable format.
        content_type : str
            MIME type of the upload (diagnostic only; ffmpeg sniffs the format).

        Returns
        -------
        SttResult
            Recognised text (empty for silence) and ``engine="vosk"``.

        Raises
        ------
        SttUnavailableError
            vosk, the model directory, or ffmpeg is missing.
        SttTranscriptionError
            The audio could not be decoded.
        """
        with self._lock:
            model = self._load_model()
            pcm = _to_pcm(audio)
            recognizer = vosk.KaldiRecognizer(model, _SAMPLE_RATE_HZ)
            for start in range(0, len(pcm), _PCM_CHUNK_BYTES):
                recognizer.AcceptWaveform(pcm[start : start + _PCM_CHUNK_BYTES])
            result = json.loads(recognizer.FinalResult())
        text = (result.get("text") or "").strip()
        logger.info(
            "transcribed %d byte(s) of %s to %d character(s)", len(audio), content_type, len(text)
        )
        return SttResult(text=text, engine=self.name)
