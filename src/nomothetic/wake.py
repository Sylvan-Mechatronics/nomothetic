"""On-robot wake-word voice commands (Phase 29, ADR-021).

The robot listens on its own USB microphone for a configurable catch phrase
(``NOMON_WAKE_PHRASE``, e.g. ``"hey nomon"``), plays a chime to signal it is
listening, captures the spoken command, transcribes it with the *shared* Vosk
model (:meth:`nomothetic.stt.VoskSttEngine.create_recognizer`), and dispatches
the text to the same :class:`~nomothetic.ai_command.AiCommandService` the app's
chat bar uses.  Like the AI relay, this is *operator convenience, not
autonomy* — no cognition or robot state lives here (ADR-004), and autonomon is
uninvolved.

Detection uses a keyword-grammar recognizer (the phrase, its configured
variants, and ``"[unk]"``), which keeps continuous decoding cheap on the
Pi Zero 2W; a cheap RMS energy gate skips decoding entirely during silence.
After a command's reply, the listener keeps the conversation open for a
follow-up window (``NOMON_WAKE_FOLLOWUP_WINDOW_S``) so the operator can issue
another command without re-waking; silence resets the conversation.

Feedback is chimes (wake / processing / success / error) synthesised as WAV
files under ``$NOMON_MEDIA_DIR/audio/chimes/`` on first start — replace those
files to customise the sounds.  The processing blip plays as soon as an
utterance is captured, covering the otherwise-silent gap while the transcript
is finalised and the AI command runs.  When a :class:`~nomothetic.tts.TtsEngine`
is wired, the listener also *speaks the heard transcript back* over that gap —
synthesis runs on a side thread concurrently with the network-bound AI
dispatch, so the echo overlaps the wait rather than adding to it.  The speaker
amplifier is enabled around each clip via the nomopractic HAT daemon,
mirroring the ``POST /api/audio/play`` flow.

``pyaudio`` is an optional dependency (``nomothetic[audio]``); without it — or
without vosk and its model (``nomothetic[stt]``) — the listener logs a warning
and stays disabled instead of failing the API (ADR on conditional imports).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import operator
import os
import threading
import time
import wave
from array import array
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, Protocol

from nomothetic.ai_command import AiProviderError, AiUnavailableError
from nomothetic.audio import (
    DEFAULT_DEVICE_NAME_MATCH,
    env_device_index,
    resolve_input_device,
    resolve_output_device,
)
from nomothetic.hat import HatConnectionError, HatError
from nomothetic.stt import SttUnavailableError
from nomothetic.tts import TtsUnavailableError

try:
    import pyaudio  # type: ignore[import-untyped]

    _PYAUDIO_AVAILABLE = True
except ImportError:  # pragma: no cover — pyaudio not installed in dev env
    pyaudio = None  # type: ignore[assignment]
    _PYAUDIO_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sample rate the recognizers are fed (matches the Vosk model).
_TARGET_RATE_HZ = 16000

#: Capture rates probed on the mic, in order of preference.  PortAudio does
#: not resample on raw ``hw`` devices, and the PCM2902 may only expose
#: 44.1/48 kHz — anything above 16 kHz is downsampled in software.
_CAPTURE_RATES_HZ = (16000, 48000, 44100)

#: Anti-alias low-pass cutoff for the software downsampler, below the 8 kHz
#: Nyquist of the 16 kHz target.  Speech carries little above 7 kHz, so this
#: keeps most sibilants while suppressing the noise that would otherwise fold
#: into the band.
_RESAMPLE_CUTOFF_HZ = 7000

#: Windowed-sinc FIR length (odd) for the downsampler.  25 taps with a Blackman
#: window puts the deep fold band (≥10 kHz → 0–6 kHz speech core) well below
#: -25 dB.  The resample is skipped during the RMS-gated silence (see
#: ``_listen_for_wake``), so this cost is only paid on frames that carry speech.
_RESAMPLE_TAPS = 25

#: Duration of one capture frame.
_FRAME_MS = 100

#: Conversation history cap (matches the app's client-side trim; the REST
#: route allows 40, so 20 leaves the same headroom the app keeps).
_MAX_HISTORY_MESSAGES = 20

#: Frames of below-threshold audio before the RMS gate stops feeding the
#: wake recognizer (10 × 100 ms = 1 s of hangover).
_GATE_HOLD_FRAMES = 10

#: Mic-open retry backoff bounds (seconds).
_MIC_RETRY_BASE_S = 1.0
_MIC_RETRY_CAP_S = 30.0

#: Poll interval while parked in the paused state.
_PAUSE_POLL_S = 0.1

#: Chime synthesis parameters.
_CHIME_RATE_HZ = 44100
_CHIME_AMPLITUDE = 0.4
_CHIME_FADE_MS = 5

#: Chime name → tuple of (frequency_hz, duration_s) tones, played in order.
_CHIME_SPECS: dict[str, tuple[tuple[float, float], ...]] = {
    "wake": ((880.0, 0.12), (1174.7, 0.12)),
    "processing": ((987.8, 0.10),),
    "success": ((1046.5, 0.09), (1318.5, 0.09), (1568.0, 0.09)),
    "error": ((622.3, 0.15), (415.3, 0.15)),
}

_DEFAULT_COMMAND_TIMEOUT_S = 8.0
_DEFAULT_MAX_UTTERANCE_S = 15.0
_DEFAULT_FOLLOWUP_WINDOW_S = 8.0
_DEFAULT_AI_TIMEOUT_S = 120.0
_DEFAULT_RMS_THRESHOLD = 300
_DEFAULT_CHIME_VOLUME_PCT = 80
_DEFAULT_MIC_GAIN_PCT = 80


# ---------------------------------------------------------------------------
# Dependency protocols (kept minimal so tests can inject plain fakes)
# ---------------------------------------------------------------------------


class RecognizerProvider(Protocol):
    """Provides streaming recognizers sharing one loaded model (VoskSttEngine)."""

    def create_recognizer(self, sample_rate_hz: float = ..., grammar_json: str | None = ...) -> Any:
        """Return a ``KaldiRecognizer``-shaped object."""
        ...  # pragma: no cover - protocol definition

    def unload(self) -> bool:
        """Release the loaded model, if any, freeing its memory."""
        ...  # pragma: no cover - protocol definition


class CommandRunner(Protocol):
    """Runs one chat command through the AI tool loop (AiCommandService)."""

    async def run_command(self, messages: list[dict[str, Any]], api_key: str) -> dict[str, Any]:
        """Return ``{reply, actions, model, stop_reason}``."""
        ...  # pragma: no cover - protocol definition


class KeyResolver(Protocol):
    """Resolves the effective Anthropic API key (AiKeyStore)."""

    def resolve(self) -> tuple[str, str] | None:
        """Return ``(key, source)`` or ``None`` when unconfigured."""
        ...  # pragma: no cover - protocol definition


class SpeechSynthesizer(Protocol):
    """Synthesises spoken audio for the transcript echo (TtsEngine)."""

    def synthesize(self, text: str) -> bytes:
        """Return WAV bytes speaking *text* (``b""`` for empty input)."""
        ...  # pragma: no cover - protocol definition


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    """Read a float env var, falling back to *default* on junk or out-of-range."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if not lo <= value <= hi:
        logger.warning("%s=%s is outside [%s, %s]; using %s", name, value, lo, hi, default)
        return default
    return value


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    """Read an int env var, falling back to *default* on junk or out-of-range."""
    return int(_float_env(name, float(default), float(lo), float(hi)))


def _default_chime_volume_pct() -> int:
    """Chime volume: ``NOMON_WAKE_CHIME_VOLUME_PCT`` → ``NOMON_AUDIO_VOLUME`` → 80."""
    for name in ("NOMON_WAKE_CHIME_VOLUME_PCT", "NOMON_AUDIO_VOLUME"):
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError:
                logger.warning("%s=%r is not an integer; ignoring", name, raw)
                continue
            if 0 <= value <= 100:
                return value
            logger.warning("%s=%d is outside [0, 100]; ignoring", name, value)
    return _DEFAULT_CHIME_VOLUME_PCT


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _normalize_phrase(text: str) -> str:
    """Lowercase and collapse whitespace so phrase comparisons are stable."""
    return " ".join(text.lower().split())


def parse_variants(raw: str | Sequence[str]) -> list[str]:
    """Normalise a comma-separated string (or sequence) of phrase variants.

    Parameters
    ----------
    raw : str | Sequence[str]
        Either the raw ``NOMON_WAKE_PHRASE_VARIANTS`` value (comma-separated)
        or an already-split sequence.

    Returns
    -------
    list[str]
        Normalised, de-duplicated, non-empty variants in input order.
    """
    parts: Sequence[str] = raw.split(",") if isinstance(raw, str) else raw
    variants: list[str] = []
    for part in parts:
        normalized = _normalize_phrase(part)
        if normalized and normalized not in variants:
            variants.append(normalized)
    return variants


def _build_grammar(phrase: str, variants: Sequence[str]) -> str:
    """Build the Vosk keyword-grammar JSON for the wake recognizer.

    The grammar limits recognition to the catch phrase, its variants, and the
    ``"[unk]"`` catch-all — which keeps decoding cheap and everything else
    transcribed as ``[unk]`` instead of a false wake.

    Parameters
    ----------
    phrase : str
        The primary catch phrase.
    variants : Sequence[str]
        Alternate in-vocabulary spellings that also count as a wake.

    Returns
    -------
    str
        JSON array string for ``KaldiRecognizer``'s grammar argument.
    """
    entries: list[str] = []
    for candidate in (phrase, *variants):
        normalized = _normalize_phrase(candidate)
        if normalized and normalized not in entries:
            entries.append(normalized)
    entries.append("[unk]")
    return json.dumps(entries)


def _strip_unk(text: str) -> str:
    """Drop ``[unk]`` tokens from a grammar-recognizer transcript."""
    return " ".join(word for word in text.split() if word != "[unk]")


def _design_lowpass(src_rate_hz: int, cutoff_hz: float, num_taps: int) -> list[float]:
    """Design a windowed-sinc low-pass FIR, normalised to unity DC gain.

    Parameters
    ----------
    src_rate_hz : int
        Sample rate the filter runs at.
    cutoff_hz : float
        -6 dB cutoff frequency.
    num_taps : int
        Filter length (use an odd number for a symmetric linear-phase kernel).

    Returns
    -------
    list[float]
        Filter coefficients summing to 1.0.
    """
    fc = cutoff_hz / src_rate_hz  # normalised cutoff, cycles/sample
    center = (num_taps - 1) / 2.0
    taps: list[float] = []
    for i in range(num_taps):
        x = i - center
        sinc = 2.0 * fc if x == 0 else math.sin(2.0 * math.pi * fc * x) / (math.pi * x)
        # Blackman window — deep stopband so aliased noise is well suppressed.
        window = (
            0.42
            - 0.5 * math.cos(2.0 * math.pi * i / (num_taps - 1))
            + 0.08 * math.cos(4.0 * math.pi * i / (num_taps - 1))
        )
        taps.append(sinc * window)
    total = sum(taps)
    return [t / total for t in taps]


class _Resampler:
    """Streaming anti-aliased downsampler to 16 kHz for the Vosk recognizers.

    The naive alternative — decimating linear interpolation with no low-pass —
    folds everything above 8 kHz (mic hiss, sibilance, USB noise) back into the
    speech band, degrading recognition; the phone upload path avoids this by
    resampling through ffmpeg.  This mirrors that: a windowed-sinc low-pass at
    the source rate, then linear interpolation to 16 kHz on the now
    band-limited signal.

    Stateful so it can run per 100 ms frame without boundary transients — the
    last ``num_taps - 1`` input samples carry over as filter history, so a
    stream processed in chunks matches processing it whole.  Pure Python on
    ``array('h')`` (no numpy); the decimating convolution evaluates the filter
    only at the output positions.  A 48 kHz frame costs a few tens of ms on the
    Pi Zero 2W — affordable per speech frame, but the caller gates it behind
    the RMS energy check so it never runs during silence.

    Parameters
    ----------
    src_rate_hz : int
        Sample rate of the input PCM.  Equal to ``target_rate_hz`` means
        identity passthrough.
    target_rate_hz : int
        Output sample rate (default 16 kHz).
    """

    def __init__(self, src_rate_hz: int, target_rate_hz: int = _TARGET_RATE_HZ) -> None:
        self._src = src_rate_hz
        self._target = target_rate_hz
        self._identity = src_rate_hz == target_rate_hz
        if self._identity:
            self._taps: list[float] = []
            self._ntaps = 0
            self._step = 1.0
            self._history = array("h")
            return
        self._taps = _design_lowpass(src_rate_hz, _RESAMPLE_CUTOFF_HZ, _RESAMPLE_TAPS)
        self._ntaps = len(self._taps)
        self._step = src_rate_hz / target_rate_hz
        self._history = array("h", bytes(2 * (self._ntaps - 1)))  # zero-primed

    def reset(self) -> None:
        """Clear filter history (call when the capture stream restarts)."""
        if not self._identity:
            self._history = array("h", bytes(2 * (self._ntaps - 1)))

    def process(self, pcm: bytes) -> bytes:
        """Filter and downsample one chunk of 16-bit mono PCM."""
        if self._identity:
            return pcm
        x = array("h")
        x.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if not x:
            return b""
        ntaps = self._ntaps
        taps = self._taps
        combined = self._history + x
        buf = combined.tolist()  # list indexing beats array in the hot loop
        # Carry the last (ntaps - 1) input samples for the next chunk.
        self._history = combined[-(ntaps - 1) :]
        n_out = int(round(len(x) * self._target / self._src))
        step = self._step
        out = array("h", bytes(2 * n_out))
        mul = operator.mul
        for i in range(n_out):
            pos = i * step
            idx = int(pos)
            frac = pos - idx
            # Filtered value at source index idx: dot(taps, buf[idx : idx+ntaps]).
            value = sum(map(mul, taps, buf[idx : idx + ntaps]))
            if frac:
                nxt = sum(map(mul, taps, buf[idx + 1 : idx + 1 + ntaps]))
                value = value * (1.0 - frac) + nxt * frac
            sample = int(value)
            if sample > 32767:
                sample = 32767
            elif sample < -32768:
                sample = -32768
            out[i] = sample
        return out.tobytes()


def _resample_to_16k(pcm: bytes, src_rate_hz: int) -> bytes:
    """One-shot anti-aliased downsample of 16-bit mono PCM to 16 kHz.

    Convenience wrapper over a fresh :class:`_Resampler`; the streaming capture
    path uses a persistent resampler so frames join without boundary artifacts.

    Parameters
    ----------
    pcm : bytes
        Little-endian signed 16-bit mono samples.
    src_rate_hz : int
        Sample rate of *pcm*; returned unchanged when already 16 kHz.

    Returns
    -------
    bytes
        16 kHz little-endian signed 16-bit mono samples.
    """
    return _Resampler(src_rate_hz).process(pcm)


def _rms(pcm: bytes) -> float:
    """Root-mean-square amplitude of 16-bit mono PCM (0 for empty input)."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _trim_history(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Cap conversation history, dropping the oldest turns in pairs.

    Dropping user/assistant *pairs* preserves the user-first alternation the
    Messages API requires (validation lives only in the REST route, so the
    listener must keep its history well-formed itself).
    """
    trimmed = list(messages)
    while len(trimmed) > _MAX_HISTORY_MESSAGES:
        del trimmed[:2]
    return trimmed


def synthesize_chimes(chime_dir: Path) -> dict[str, Path]:
    """Write any missing chime WAVs under *chime_dir* and return name → path.

    Existing files are left untouched so operators can drop in their own
    ``wake.wav`` / ``processing.wav`` / ``success.wav`` / ``error.wav``.
    Chimes are short tone sequences (16-bit mono, 44.1 kHz) with 5 ms fades
    to avoid clicks.

    Parameters
    ----------
    chime_dir : Path
        Directory for the chime files (created if missing); normally
        ``$NOMON_MEDIA_DIR/audio/chimes``.

    Returns
    -------
    dict[str, Path]
        Mapping of chime name (``wake``/``processing``/``success``/``error``)
        to WAV path.

    Raises
    ------
    OSError
        The directory or a WAV file could not be written.
    """
    chime_dir.mkdir(parents=True, exist_ok=True)
    fade = int(_CHIME_RATE_HZ * _CHIME_FADE_MS / 1000)
    paths: dict[str, Path] = {}
    for name, spec in _CHIME_SPECS.items():
        path = chime_dir / f"{name}.wav"
        paths[name] = path
        if path.exists():
            continue
        pcm = array("h")
        for frequency_hz, duration_s in spec:
            n = int(_CHIME_RATE_HZ * duration_s)
            for i in range(n):
                value = _CHIME_AMPLITUDE * math.sin(2 * math.pi * frequency_hz * i / _CHIME_RATE_HZ)
                if i < fade:
                    value *= i / fade
                elif i >= n - fade:
                    value *= (n - 1 - i) / fade
                pcm.append(int(value * 32767))
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(_CHIME_RATE_HZ)
            wf.writeframes(pcm.tobytes())
        logger.info("synthesised chime %s", path)
    return paths


# ---------------------------------------------------------------------------
# WakeWordListener
# ---------------------------------------------------------------------------


class WakeWordListener:
    """Always-on wake-word listener driving hands-free AI commands.

    One daemon thread owns the microphone and walks a small state machine:
    listen for the wake phrase (grammar recognizer, RMS-gated) → wake chime →
    capture an utterance (full-vocabulary recognizer with Vosk endpointing) →
    dispatch to the AI service on the app's event loop → success/error chime →
    follow-up window (capture again, history carried) → reset on silence.

    The mic stream is *closed* while chimes play (no self-hearing) and while
    the AI executes (the robot may move); the existing ``/api/audio/record``
    endpoints pause/resume the listener so recordings never contend for the
    ALSA device.

    Construction is side-effect free — no pyaudio, vosk, or filesystem access
    happens until :meth:`start_background`, so the listener is always safe to
    build, even on dev machines without the optional extras.

    Parameters
    ----------
    stt_engine : RecognizerProvider
        Engine providing shared-model streaming recognizers
        (:class:`~nomothetic.stt.VoskSttEngine`).
    ai_service : CommandRunner
        The app's :class:`~nomothetic.ai_command.AiCommandService`.
    ai_key_store : KeyResolver
        The app's :class:`~nomothetic.ai_command.AiKeyStore`.
    get_player : callable
        Zero-arg callable returning the shared
        :class:`~nomothetic.audio.AudioPlayer` (or ``None``) — used only to
        avoid fighting REST playback over the DAC/amplifier.
    get_hat : callable
        Zero-arg callable returning the :class:`~nomothetic.hat.HatClient`
        (or ``None``) for best-effort amp enable / volume / mic gain.
    get_chime_dir : callable
        Zero-arg callable returning the chime directory.
    tts_engine : SpeechSynthesizer, optional
        Speech synthesiser (:class:`~nomothetic.tts.EspeakTtsEngine`) used to
        echo the heard transcript through the speaker *while the AI command is
        dispatched*, covering the silent gap.  ``None`` (the default) disables
        the spoken echo; the processing chime still plays.
    phrase, variants, input_device_index, input_name_match, output_device_index,
    output_name_match, command_timeout_s, max_utterance_s, followup_window_s,
    ai_timeout_s, chime_volume_pct, rms_threshold :
        Optional overrides; each defaults from its ``NOMON_WAKE_*``
        environment variable (see ``.env.device.example``).
        Device indexes are explicit **PortAudio** indexes; when None the
        devices are auto-detected by name: capture picks the first input
        device whose name contains ``input_name_match`` (default ``"usb"``,
        falling back to the system default input), and chime playback picks
        the first output device matching ``output_name_match`` (defaulting
        to the input match — the speaker usually shares the USB codec —
        falling back to the system default output).
    """

    def __init__(
        self,
        stt_engine: RecognizerProvider,
        ai_service: CommandRunner,
        ai_key_store: KeyResolver,
        get_player: Callable[[], Any],
        get_hat: Callable[[], Any],
        get_chime_dir: Callable[[], Path],
        phrase: str | None = None,
        variants: Sequence[str] | None = None,
        input_device_index: int | None = None,
        input_name_match: str | None = None,
        output_device_index: int | None = None,
        output_name_match: str | None = None,
        command_timeout_s: float | None = None,
        max_utterance_s: float | None = None,
        followup_window_s: float | None = None,
        ai_timeout_s: float | None = None,
        chime_volume_pct: int | None = None,
        rms_threshold: int | None = None,
        tts_engine: SpeechSynthesizer | None = None,
    ) -> None:
        self._stt_engine = stt_engine
        self._ai_service = ai_service
        self._ai_key_store = ai_key_store
        self._get_player = get_player
        self._get_hat = get_hat
        self._get_chime_dir = get_chime_dir
        self._tts_engine = tts_engine

        if phrase is None:
            phrase = os.environ.get("NOMON_WAKE_PHRASE", "")
        self._phrase = _normalize_phrase(phrase)
        if variants is None:
            self._variants = parse_variants(os.environ.get("NOMON_WAKE_PHRASE_VARIANTS", ""))
        else:
            self._variants = parse_variants(variants)
        self._input_device_index = (
            input_device_index
            if input_device_index is not None
            else env_device_index("NOMON_WAKE_INPUT_INDEX")
        )
        if input_name_match is None:
            input_name_match = os.environ.get("NOMON_WAKE_INPUT_NAME", "")
        self._input_name_match = _normalize_phrase(input_name_match) or DEFAULT_DEVICE_NAME_MATCH
        self._output_device_index = (
            output_device_index
            if output_device_index is not None
            else env_device_index("NOMON_WAKE_OUTPUT_INDEX")
        )
        if output_name_match is None:
            output_name_match = os.environ.get("NOMON_WAKE_OUTPUT_NAME", "")
        # The speaker usually hangs off the same USB codec as the mic, so the
        # output name match defaults to the input one.
        self._output_name_match = _normalize_phrase(output_name_match) or self._input_name_match
        self._command_timeout_s = (
            command_timeout_s
            if command_timeout_s is not None
            else _float_env("NOMON_WAKE_COMMAND_TIMEOUT_S", _DEFAULT_COMMAND_TIMEOUT_S, 1.0, 60.0)
        )
        self._max_utterance_s = (
            max_utterance_s
            if max_utterance_s is not None
            else _float_env("NOMON_WAKE_MAX_UTTERANCE_S", _DEFAULT_MAX_UTTERANCE_S, 2.0, 60.0)
        )
        self._followup_window_s = (
            followup_window_s
            if followup_window_s is not None
            else _float_env("NOMON_WAKE_FOLLOWUP_WINDOW_S", _DEFAULT_FOLLOWUP_WINDOW_S, 0.0, 120.0)
        )
        self._ai_timeout_s = (
            ai_timeout_s
            if ai_timeout_s is not None
            else _float_env("NOMON_WAKE_AI_TIMEOUT_S", _DEFAULT_AI_TIMEOUT_S, 10.0, 300.0)
        )
        self._chime_volume_pct = (
            chime_volume_pct if chime_volume_pct is not None else _default_chime_volume_pct()
        )
        self._rms_threshold = (
            rms_threshold
            if rms_threshold is not None
            else _int_env("NOMON_WAKE_RMS_THRESHOLD", _DEFAULT_RMS_THRESHOLD, 0, 32767)
        )

        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._enabled = False
        self._state = "idle"
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._stream_closed = threading.Event()
        self._stream_closed.set()
        self._chime_paths: dict[str, Path] = {}
        self._pa: Any = None
        self._stream: Any = None
        self._capture_rate = _TARGET_RATE_HZ
        self._frames_per_buffer = int(_TARGET_RATE_HZ * _FRAME_MS / 1000)
        self._resampler: _Resampler | None = None

    # ------------------------------------------------------------------
    # Public API (called from the event loop / route handlers)
    # ------------------------------------------------------------------

    @property
    def phrase(self) -> str:
        """The normalised catch phrase (empty when unconfigured)."""
        return self._phrase

    @property
    def variants(self) -> list[str]:
        """The normalised accepted phrase variants."""
        return list(self._variants)

    @property
    def followup_window_s(self) -> float:
        """Seconds the conversation stays open for follow-ups (0 = disabled)."""
        return self._followup_window_s

    @property
    def enabled(self) -> bool:
        """True when the listener has been started and not stopped/failed."""
        return self._enabled

    @property
    def is_running(self) -> bool:
        """True while the listener thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def is_paused(self) -> bool:
        """True while the listener is parked off the microphone."""
        return self._pause_event.is_set()

    @property
    def state(self) -> str:
        """Diagnostic state: idle/listening/capturing/dispatching/paused/…"""
        return self._state

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the event loop AI dispatches are scheduled onto (lifespan)."""
        self._loop = loop

    def set_phrase_config(
        self, phrase: str | None = None, variants: Sequence[str] | None = None
    ) -> None:
        """Update the catch phrase and/or variants.

        Takes effect on the next :meth:`start_background` — callers changing a
        *running* listener must stop it first (the recognizer grammar is built
        at thread start).  Runtime updates are in-memory only; the boot values
        come from the environment.
        """
        if phrase is not None:
            self._phrase = _normalize_phrase(phrase)
        if variants is not None:
            self._variants = parse_variants(variants)

    def start_background(self, loop: asyncio.AbstractEventLoop | None = None) -> bool:
        """Start the listener thread; returns False when prerequisites are missing.

        Missing pyaudio, an empty phrase, or no attached event loop each log a
        warning and return ``False`` rather than raising — the API must come up
        on devices (and dev machines) without the audio stack (ADR-004).

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop, optional
            Event loop for AI dispatches; defaults to the loop passed to
            :meth:`attach_loop`.

        Returns
        -------
        bool
            True when the thread is running (or already was).
        """
        with self._lock:
            if loop is not None:
                self._loop = loop
            if self._thread is not None and self._thread.is_alive():
                return True
            if not _PYAUDIO_AVAILABLE:
                logger.warning(
                    "wake-word listener unavailable: pyaudio is not installed "
                    "(nomothetic[audio] extra)"
                )
                return False
            if not self._phrase:
                logger.warning("wake-word listener not started: no wake phrase configured")
                return False
            if self._loop is None:
                logger.warning("wake-word listener not started: no event loop attached")
                return False
            try:
                self._chime_paths = synthesize_chimes(self._get_chime_dir())
            except OSError as e:
                logger.warning("wake chime synthesis failed (%s); chimes disabled", e)
                self._chime_paths = {}
            self._stop_event.clear()
            self._pause_event.clear()
            self._enabled = True
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="nomon-wake-listener"
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        """Stop the listener thread and release the microphone (idempotent)."""
        with self._lock:
            self._enabled = False
            thread = self._thread
        self._stop_event.set()
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():  # pragma: no cover - defensive
                logger.warning("wake-word listener thread did not stop within 5s")
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def unload_model(self) -> bool:
        """Release the shared STT model's memory once the listener is stopped.

        Call this only after :meth:`stop` has returned — while the thread is
        running, its recognizers hold a raw handle into the model rather
        than a Python reference, so freeing it out from under them would be
        a use-after-free, not a graceful degrade. This is a no-op (logged)
        if the thread is still alive. The model reloads lazily, at the
        usual multi-second cost, on next use anywhere — this listener
        restarting, or an app-initiated ``/api/ai/transcribe`` upload —
        since it is shared with that endpoint
        (:class:`~nomothetic.stt.VoskSttEngine`).

        Returns
        -------
        bool
            True if a model was loaded and has now been released.
        """
        if self.is_running:
            logger.warning("not unloading STT model: wake-word listener thread is still running")
            return False
        return self._stt_engine.unload()

    def pause(self, timeout_s: float = 3.0) -> None:
        """Park the listener off the microphone (e.g. for a REST recording).

        Blocks until the mic stream is closed (or *timeout_s* elapses); any
        in-flight conversation is abandoned without a chime.  The stream stays
        closed until :meth:`resume`.
        """
        self._pause_event.set()
        if not self._stream_closed.wait(timeout_s):  # pragma: no cover - defensive
            logger.warning(
                "wake-word listener did not release the microphone within %ss", timeout_s
            )

    def resume(self) -> None:
        """Resume wake listening after :meth:`pause`."""
        self._pause_event.clear()

    # ------------------------------------------------------------------
    # Listener thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Thread body: recognizer setup, mic lifecycle, and the state machine."""
        try:
            wake_recognizer = self._stt_engine.create_recognizer(
                _TARGET_RATE_HZ, _build_grammar(self._phrase, self._variants)
            )
            command_recognizer = self._stt_engine.create_recognizer(_TARGET_RATE_HZ)
        except SttUnavailableError as e:
            logger.warning("wake-word listener disabled: %s", e)
            self._enabled = False
            self._state = "unavailable"
            return
        except Exception:  # noqa: BLE001 - never crash the API over the listener
            logger.exception("wake-word listener failed to initialise recognizers")
            self._enabled = False
            self._state = "unavailable"
            return

        logger.info(
            'wake-word listener started (phrase="%s", %d variant(s), followup=%.0fs)',
            self._phrase,
            len(self._variants),
            self._followup_window_s,
        )
        retry_delay = _MIC_RETRY_BASE_S
        try:
            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    self._close_stream()
                    self._state = "paused"
                    while self._pause_event.is_set() and not self._stop_event.is_set():
                        self._stop_event.wait(_PAUSE_POLL_S)
                    continue
                if self._stream is None:
                    try:
                        self._open_stream()
                        retry_delay = _MIC_RETRY_BASE_S
                    except Exception as e:  # noqa: BLE001 - mic may be unplugged
                        logger.warning(
                            "wake-word microphone unavailable (%s); retrying in %.0fs",
                            e,
                            retry_delay,
                        )
                        self._state = "mic_unavailable"
                        self._stop_event.wait(retry_delay)
                        retry_delay = min(retry_delay * 2, _MIC_RETRY_CAP_S)
                        continue
                self._state = "listening"
                if self._listen_for_wake(wake_recognizer):
                    logger.info("wake phrase detected")
                    self._run_conversation(command_recognizer)
        except Exception:  # noqa: BLE001 - never crash the API over the listener
            logger.exception("wake-word listener crashed")
            self._enabled = False
        finally:
            self._close_stream()
            self._reset_pyaudio()
            self._state = "stopped"

    def _resolve_input_device(self, pa: Any) -> int | None:
        """Pick the capture device (shared name-match logic in nomothetic.audio)."""
        return resolve_input_device(
            pa, name_match=self._input_name_match, explicit_index=self._input_device_index
        )

    def _resolve_output_device(self, pa: Any) -> int | None:
        """Pick the chime output device (shared name-match logic in nomothetic.audio).

        On robots whose default output is a silent HDMI card, the name match
        routes chimes to the USB codec the speaker amplifier is wired to.
        """
        return resolve_output_device(
            pa, name_match=self._output_name_match, explicit_index=self._output_device_index
        )

    def _ensure_pyaudio(self) -> Any:
        """Return the persistent PyAudio host, constructing it if needed.

        One host is kept for the listener's lifetime instead of one per
        stream-open / chime / TTS clip — each ``pyaudio.PyAudio()`` call
        re-enumerates every ALSA device, which otherwise happens several
        times per conversation turn.  Only :meth:`_reset_pyaudio` tears it
        down (a failed open, or thread shutdown), so a replugged USB device
        is still noticed on the next retry rather than staying invisible for
        the listener's whole lifetime.
        """
        pa = self._pa
        if pa is None:
            pa = self._pa = pyaudio.PyAudio()
        return pa

    def _reset_pyaudio(self) -> None:
        """Tear down the persistent PyAudio host, if any (idempotent)."""
        pa, self._pa = self._pa, None
        if pa is not None:
            try:
                pa.terminate()
            except Exception:  # noqa: BLE001 - releasing best-effort
                pass

    def _open_stream(self) -> None:
        """Open the mic, probing capture rates; applies best-effort mic gain."""
        pa = self._ensure_pyaudio()
        try:
            device_index = self._resolve_input_device(pa)
            if device_index is None:
                raise OSError("no usable capture device found")
            last_error: Exception | None = None
            for rate in _CAPTURE_RATES_HZ:
                frames = max(1, int(rate * _FRAME_MS / 1000))
                try:
                    stream = pa.open(
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=rate,
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=frames,
                    )
                except Exception as e:  # noqa: BLE001 - rate unsupported / device busy
                    last_error = e
                    continue
                self._stream = stream
                self._capture_rate = rate
                self._frames_per_buffer = frames
                # Fresh resampler per stream — clears filter history so a gap
                # between streams (chime, pause) can't bleed stale samples in.
                self._resampler = _Resampler(rate)
                self._stream_closed.clear()
                logger.info("wake-word microphone open (device %d at %d Hz)", device_index, rate)
                self._apply_mic_gain()
                return
            raise last_error if last_error is not None else OSError("no usable capture rate")
        except Exception:
            # Tear the host down too, not just the failed stream attempt — if
            # the failure is a vanished/replugged device, the next attempt
            # needs a fresh enumeration, not the same stale device table.
            self._reset_pyaudio()
            raise

    def _close_stream(self) -> None:
        """Release the mic stream (safe to call repeatedly).

        Leaves the persistent PyAudio host (``self._pa``) open — only the
        stream is torn down, so pause/resume and chime playback don't each
        pay for a full host re-enumeration (see :meth:`_ensure_pyaudio`).
        """
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:  # noqa: BLE001 - releasing best-effort
                pass
        self._stream_closed.set()

    def _read_raw_frame(self) -> bytes | None:
        """Read one capture frame at the native rate; None (closed) on error."""
        stream = self._stream
        if stream is None:
            return None
        try:
            data: bytes = stream.read(self._frames_per_buffer, exception_on_overflow=False)
        except Exception as e:  # noqa: BLE001 - mic unplugged mid-read
            logger.warning("wake-word microphone read failed: %s", e)
            self._close_stream()
            return None
        return data

    def _resample(self, raw: bytes) -> bytes:
        """Downsample a native-rate frame to 16 kHz for the recognizers."""
        resampler = self._resampler
        if resampler is None:  # defensive: stream open implies resampler set
            resampler = self._resampler = _Resampler(self._capture_rate)
        return resampler.process(raw)

    def _read_frame(self) -> bytes | None:
        """Read one capture frame as 16 kHz PCM; None (stream closed) on error."""
        raw = self._read_raw_frame()
        return None if raw is None else self._resample(raw)

    def _listen_for_wake(self, recognizer: Any) -> bool:
        """Feed the grammar recognizer until the wake phrase is heard.

        The RMS energy gate runs on the *raw* (native-rate) frame so sustained
        silence skips the anti-alias resample entirely, not just the decode —
        keeping idle CPU near zero.  Speech resuming after a gated gap resets
        the resampler so stale filter history can't bleed into the first frame.

        Returns False when interrupted (stop/pause) or on a stream error —
        the outer loop handles reopening/backoff.
        """
        silent_frames = 0
        fed_since_finalize = False
        gated = False
        while not self._stop_event.is_set() and not self._pause_event.is_set():
            raw = self._read_raw_frame()
            if raw is None:
                return False
            if self._rms_threshold > 0:
                if _rms(raw) < self._rms_threshold:
                    silent_frames += 1
                    if silent_frames >= _GATE_HOLD_FRAMES:
                        # Gate engaged: skip resample + decode, reset once.
                        if fed_since_finalize:
                            recognizer.Reset()
                            fed_since_finalize = False
                        gated = True
                        continue
                else:
                    if gated:
                        # Speech resumed after a gated gap — drop stale history.
                        self._reset_resampler()
                        gated = False
                    silent_frames = 0
            frame = self._resample(raw)
            if recognizer.AcceptWaveform(frame):
                fed_since_finalize = False
                raw_text = str(json.loads(recognizer.Result()).get("text") or "")
                if self._matches_wake(raw_text):
                    return True
            else:
                fed_since_finalize = True
        return False

    def _reset_resampler(self) -> None:
        """Clear resampler filter history (best-effort)."""
        if self._resampler is not None:
            self._resampler.reset()

    def _matches_wake(self, raw_text: str) -> bool:
        """True when a grammar-recognizer transcript counts as the wake phrase."""
        cleaned = _strip_unk(_normalize_phrase(raw_text))
        if not cleaned:
            return False
        for target in (self._phrase, *self._variants):
            if cleaned == target or cleaned.endswith(" " + target):
                return True
        return False

    def _capture_utterance(self, recognizer: Any, timeout_s: float, max_s: float) -> str | None:
        """Capture one spoken utterance with Vosk endpointing.

        Parameters
        ----------
        recognizer : Any
            The full-vocabulary recognizer.
        timeout_s : float
            How long to wait for speech to *start*; once a partial transcript
            appears the utterance may run up to *max_s*.
        max_s : float
            Hard cap — recognition is force-finalised at this point.

        Returns
        -------
        str | None
            The transcript, or None on silence/timeout/interruption.
        """
        start = time.monotonic()
        speech_started = False
        while not self._stop_event.is_set() and not self._pause_event.is_set():
            elapsed = time.monotonic() - start
            if elapsed >= max_s:
                text = str(json.loads(recognizer.FinalResult()).get("text") or "").strip()
                return text or None
            if elapsed >= timeout_s and not speech_started:
                recognizer.Reset()
                return None
            frame = self._read_frame()
            if frame is None:
                return None
            if recognizer.AcceptWaveform(frame):
                text = str(json.loads(recognizer.Result()).get("text") or "").strip()
                if text:
                    return text
                speech_started = False  # noise finalised to nothing; keep waiting
            elif not speech_started:
                partial = str(json.loads(recognizer.PartialResult()).get("partial") or "")
                if partial.strip():
                    speech_started = True
        recognizer.Reset()
        return None

    def _run_conversation(self, command_recognizer: Any) -> None:
        """One wake-initiated conversation: capture → dispatch → follow-ups."""
        history: list[dict[str, str]] = []
        self._close_stream()
        self._play_chime("wake")
        timeout_s = self._command_timeout_s
        while not self._stop_event.is_set() and not self._pause_event.is_set():
            if not self._reopen_for_capture():
                return
            self._state = "capturing"
            text = self._capture_utterance(command_recognizer, timeout_s, self._max_utterance_s)
            interrupted = self._stop_event.is_set() or self._pause_event.is_set()
            self._close_stream()
            if interrupted:
                return
            if text is None:
                if not history:
                    # Heard nothing after the wake chime at all.
                    logger.info("wake conversation ended: no command heard")
                    self._play_chime("error")
                else:
                    logger.info("wake conversation ended: follow-up window expired")
                return
            logger.info('wake command heard: "%s"', text)
            # Ack blip: the utterance was captured and is now being processed
            # (transcription finalised, AI dispatch in flight) — without it the
            # operator hears nothing until the success/error chime.
            self._play_chime("processing")
            history.append({"role": "user", "content": text})
            history = _trim_history(history)
            self._state = "dispatching"
            # Speak the transcript back concurrently with the network-bound AI
            # dispatch, so the echo overlaps the wait instead of adding to it.
            speaker = self._speak_async(text)
            reply = self._dispatch(history)
            self._await_speech(speaker)
            if reply is None:
                self._play_chime("error")
                return
            self._play_chime("success")
            if not reply or self._followup_window_s <= 0:
                # No assistant text to carry forward (or follow-ups disabled).
                return
            history.append({"role": "assistant", "content": reply})
            history = _trim_history(history)
            timeout_s = self._followup_window_s

    def _dispatch(self, messages: list[dict[str, str]]) -> str | None:
        """Run the conversation through the AI service on the app's event loop.

        Returns the assistant reply text (possibly empty), or None on any
        failure — no key, loop unavailable, provider error, or timeout.
        """
        resolved = self._ai_key_store.resolve()
        if resolved is None:
            logger.warning("wake command ignored: no AI API key configured")
            return None
        api_key, _source = resolved
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning("wake command dropped: event loop unavailable")
            return None
        payload: list[dict[str, Any]] = [dict(message) for message in messages]
        future = asyncio.run_coroutine_threadsafe(
            self._ai_service.run_command(payload, api_key=api_key), loop
        )
        try:
            result = future.result(timeout=self._ai_timeout_s)
        except TimeoutError:
            future.cancel()
            logger.warning("wake command timed out after %.0fs", self._ai_timeout_s)
            return None
        except (AiUnavailableError, AiProviderError) as e:
            logger.warning("wake command failed: %s", e)
            return None
        except Exception:  # noqa: BLE001 - never crash the listener over one command
            logger.exception("wake command failed unexpectedly")
            return None
        reply = str(result.get("reply") or "")
        actions = result.get("actions") or []
        logger.info(
            'wake command completed: %d action(s), stop_reason=%s, reply="%.200s"',
            len(actions),
            result.get("stop_reason"),
            reply,
        )
        return reply

    def _reopen_for_capture(self) -> bool:
        """Reopen the mic after a chime/dispatch; False when interrupted or failing."""
        if self._stop_event.is_set() or self._pause_event.is_set():
            return False
        if self._stream is not None:
            return True
        try:
            self._open_stream()
            return True
        except Exception as e:  # noqa: BLE001 - mic may have vanished mid-conversation
            logger.warning("wake-word microphone reopen failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Chimes & HAT plumbing
    # ------------------------------------------------------------------

    def _hat(self) -> Any:
        """Return the HAT client, or None when unavailable."""
        try:
            return self._get_hat()
        except Exception:  # noqa: BLE001 - accessor is best-effort
            return None

    def _apply_mic_gain(self) -> None:
        """Best-effort default mic gain via the HAT daemon (mirrors record start)."""
        hat = self._hat()
        if hat is None:
            return
        gain = _int_env("NOMON_AUDIO_MIC_GAIN", _DEFAULT_MIC_GAIN_PCT, 0, 100)
        try:
            hat.set_mic_gain(gain)
        except HatConnectionError:
            pass  # Daemon not up yet; nothing worth logging on every stream open.
        except HatError as e:
            # e.g. the configured ALSA capture control doesn't exist — the
            # daemon's message lists the card's real controls, so surface it
            # (silent capture at a wrong level is a top cause of poor STT).
            logger.warning("wake mic gain not applied (%d%%): %s", gain, e)

    def _play_chime(self, name: str) -> None:
        """Play one chime, blocking until it finishes (mic stream must be closed)."""
        self._state = "chiming"
        path = self._chime_paths.get(name)
        if path is None or not _PYAUDIO_AVAILABLE:
            return
        try:
            with wave.open(str(path), "rb") as wf:
                self._play_wave(wf)
        except Exception as e:  # noqa: BLE001 - a failed chime must not end listening
            logger.warning("chime playback failed: %s", e)

    def _speak_async(self, text: str) -> threading.Thread | None:
        """Start speaking *text* on a daemon thread; None if TTS is unavailable.

        The caller runs the AI dispatch on the listener thread meanwhile, then
        blocks on :meth:`_await_speech` so the spoken echo and the next chime
        never overlap on the speaker.
        """
        if self._tts_engine is None or not _PYAUDIO_AVAILABLE:
            return None
        thread = threading.Thread(
            target=self._speak, args=(text,), daemon=True, name="nomon-wake-tts"
        )
        thread.start()
        return thread

    def _await_speech(self, thread: threading.Thread | None) -> None:
        """Wait for a :meth:`_speak_async` thread to finish (bounded)."""
        if thread is not None:
            # Synthesis is sub-second and playback is bounded by the utterance
            # length; the cap only guards against a wedged subprocess.
            thread.join(timeout=self._max_utterance_s + 10.0)

    def _speak(self, text: str) -> None:
        """Synthesise *text* and play it once (best-effort; never raises)."""
        engine = self._tts_engine
        if engine is None or not _PYAUDIO_AVAILABLE:
            return
        self._state = "speaking"
        try:
            wav = engine.synthesize(text)
        except TtsUnavailableError as e:
            # Expected on dev machines / devices without espeak-ng or ffmpeg;
            # the processing chime already gave the operator an ack.
            logger.debug("spoken echo skipped: %s", e)
            return
        except Exception:  # noqa: BLE001 - a failed echo must not end listening
            logger.warning("speech synthesis failed", exc_info=True)
            return
        if not wav:
            return
        try:
            with wave.open(io.BytesIO(wav), "rb") as wf:
                self._play_wave(wf)
        except Exception as e:  # noqa: BLE001 - a failed echo must not end listening
            logger.warning("spoken echo playback failed: %s", e)

    def _play_wave(self, wf: Any) -> None:
        """Stream an open WAV reader to the speaker with amp management.

        Opens a dedicated output stream (rather than going through the
        shared :class:`~nomothetic.audio.AudioPlayer`) so completion timing
        is exact and REST playback can never 409 against it — but reuses the
        listener's persistent PyAudio host (:meth:`_ensure_pyaudio`) instead
        of spinning up a new one per clip.  Skipped (with a debug log) when
        REST playback holds the DAC.  The amplifier is enabled before and
        disabled after — unless REST playback is active — matching the
        ``/api/audio/play`` semantics.  Shared by chimes (called from the
        listener thread) and the spoken transcript echo (called from the TTS
        side thread) — safe because the host already exists by the time
        either can run: it's created in ``_open_stream`` before the listener
        ever reaches a state where a TTS thread could be spawned.
        """
        if not _PYAUDIO_AVAILABLE:
            return
        player = None
        try:
            player = self._get_player()
        except Exception:  # noqa: BLE001 - accessor is best-effort
            player = None
        if player is not None and getattr(player, "is_playing", False):
            logger.debug("skipping wake playback: REST audio in progress")
            return
        hat = self._hat()
        if hat is not None:
            try:
                hat.enable_speaker()
                hat.set_volume(self._chime_volume_pct)
            except (HatError, HatConnectionError):
                pass  # Still plays; the amp may already be on.
        try:
            pa = self._ensure_pyaudio()
            stream = pa.open(
                format=pa.get_format_from_width(wf.getsampwidth()),
                channels=wf.getnchannels(),
                rate=wf.getframerate(),
                output=True,
                output_device_index=self._resolve_output_device(pa),
            )
            try:
                data = wf.readframes(1024)
                while data and not self._stop_event.is_set():
                    stream.write(data)
                    data = wf.readframes(1024)
            finally:
                stream.stop_stream()
                stream.close()
        except Exception as e:  # noqa: BLE001 - a failed clip must not end listening
            logger.warning("wake playback failed: %s", e)
        finally:
            if hat is not None and not (
                player is not None and getattr(player, "is_playing", False)
            ):
                try:
                    hat.disable_speaker()
                except (HatError, HatConnectionError):
                    pass  # Amp stays on; next REST stop will disable it.
