"""Text-to-speech engines for spoken feedback.

This module gives the wake-word listener (:mod:`nomothetic.wake`, ADR-021) a
voice: when the robot hears a command it echoes the transcript back through
the speaker *while the AI command is dispatched*, filling the otherwise-silent
gap between "heard you" and the success/error chime.  Like STT and the AI
relay, this is *operator convenience, not autonomy* — no cognition or robot
state lives here (ADR-004), and autonomon is uninvolved.

The engine is pluggable behind the :class:`TtsEngine` protocol so a different
synthesiser (or a cloud voice) can be wired in ``create_app()`` without
touching the listener.  The only built-in engine is :class:`EspeakTtsEngine`:

* **Offline** — ``espeak-ng`` is a tiny formant synthesiser that runs faster
  than real time on the Pi Zero 2W, so a short phrase is ready in tens of
  milliseconds and genuinely overlaps the network-bound AI dispatch.
* **No warm-up** — there is no model to load; each call is a short subprocess.

Output is normalised to 44.1 kHz mono 16-bit WAV with an ``ffmpeg`` subprocess
(``espeak-ng`` emits its voice's native rate, usually 22.05 kHz) so the bytes
play through the exact same output path as the listener's chimes, with no
sample-rate surprises on the USB codec.  Both ``espeak-ng`` and ``ffmpeg`` are
device prerequisites for the voice stack; when either is missing this module
still imports and synthesis raises :class:`TtsUnavailableError`, which the
listener treats as "no spoken echo" rather than an error.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Protocol

logger = logging.getLogger(__name__)

#: Canonical output rate — matches the synthesised chimes so TTS and chimes
#: share the listener's PyAudio output path without a rate mismatch.
_OUTPUT_RATE_HZ = 44100

#: Default espeak-ng voice and speaking rate (words per minute).  A touch
#: slower than espeak's 175 wpm default for intelligibility over a small speaker.
_DEFAULT_VOICE = "en"
_DEFAULT_RATE_WPM = 160
_MIN_RATE_WPM = 80
_MAX_RATE_WPM = 450

_ESPEAK_TIMEOUT_S = 15.0
_FFMPEG_TIMEOUT_S = 15.0


# ============================================================================
# Errors
# ============================================================================


class TtsUnavailableError(Exception):
    """TTS cannot run on this device (missing espeak-ng or ffmpeg)."""


class TtsSynthesisError(Exception):
    """The text could not be synthesised or the audio could not be encoded."""


# ============================================================================
# Protocol
# ============================================================================


class TtsEngine(Protocol):
    """A text-to-speech backend usable by the wake-word listener."""

    name: str

    def synthesize(self, text: str) -> bytes:
        """Synthesise *text* to a playable WAV clip.

        Parameters
        ----------
        text : str
            The words to speak.  Empty/whitespace input returns ``b""``.

        Returns
        -------
        bytes
            A complete WAV file (16-bit mono), or ``b""`` for empty input.

        Raises
        ------
        TtsUnavailableError
            The engine's runtime prerequisites are missing.
        TtsSynthesisError
            Synthesis or encoding failed.
        """
        ...  # pragma: no cover - protocol definition


# ============================================================================
# espeak-ng engine
# ============================================================================


class EspeakTtsEngine:
    """Offline speech synthesis by shelling out to ``espeak-ng``.

    Parameters
    ----------
    voice : str, optional
        espeak-ng voice name.  Defaults to ``NOMON_TTS_VOICE`` then ``"en"``.
    rate_wpm : int, optional
        Speaking rate in words per minute.  Defaults to ``NOMON_TTS_RATE_WPM``
        then 160; clamped to ``[80, 450]``.
    """

    name = "espeak-ng"

    def __init__(self, voice: str | None = None, rate_wpm: int | None = None) -> None:
        self._voice = voice or os.environ.get("NOMON_TTS_VOICE", "").strip() or _DEFAULT_VOICE
        self._rate_wpm = _resolve_rate(rate_wpm)

    def synthesize(self, text: str) -> bytes:
        """Speak *text*, returning 44.1 kHz mono 16-bit WAV bytes.

        Blocking (two short subprocesses); callers on the event loop must wrap
        this in ``asyncio.to_thread``.  The wake-word listener already runs it
        on its own thread.

        Parameters
        ----------
        text : str
            The words to speak.

        Returns
        -------
        bytes
            A WAV clip, or ``b""`` when *text* is empty/whitespace.

        Raises
        ------
        TtsUnavailableError
            ``espeak-ng`` or ``ffmpeg`` is not installed on the device.
        TtsSynthesisError
            espeak-ng or ffmpeg failed, or produced no audio.
        """
        text = text.strip()
        if not text:
            return b""
        raw = self._espeak(text)
        return _normalise_wav(raw)

    def _espeak(self, text: str) -> bytes:
        """Run espeak-ng, returning its native-rate WAV on stdout.

        The text is passed as a single argv element (no shell), so there is no
        injection surface and no arg-length concern for a short command.
        """
        espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        if espeak is None:
            raise TtsUnavailableError(
                "espeak-ng is not installed on the device (apt install espeak-ng)"
            )
        try:
            proc = subprocess.run(
                [espeak, "--stdout", "-v", self._voice, "-s", str(self._rate_wpm), text],
                capture_output=True,
                timeout=_ESPEAK_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise TtsSynthesisError("speech synthesis timed out") from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip().splitlines()
            raise TtsSynthesisError(
                f"espeak-ng failed: {detail[-1] if detail else 'unknown error'}"
            )
        if not proc.stdout:
            raise TtsSynthesisError("espeak-ng produced no audio")
        logger.debug("synthesised %d character(s) to %d WAV byte(s)", len(text), len(proc.stdout))
        return proc.stdout


# ============================================================================
# Audio normalisation
# ============================================================================


def _resolve_rate(rate_wpm: int | None) -> int:
    """Resolve the speaking rate from the argument, env var, or default."""
    if rate_wpm is None:
        raw = os.environ.get("NOMON_TTS_RATE_WPM", "").strip()
        if not raw:
            return _DEFAULT_RATE_WPM
        try:
            rate_wpm = int(raw)
        except ValueError:
            logger.warning(
                "NOMON_TTS_RATE_WPM=%r is not an integer; using %d", raw, _DEFAULT_RATE_WPM
            )
            return _DEFAULT_RATE_WPM
    return max(_MIN_RATE_WPM, min(_MAX_RATE_WPM, rate_wpm))


def _normalise_wav(raw: bytes) -> bytes:
    """Transcode arbitrary WAV bytes to 44.1 kHz mono 16-bit WAV.

    Uses an ``ffmpeg`` subprocess (stdin -> stdout) so the clip plays through
    the listener's chime output path regardless of the voice's native rate.

    Parameters
    ----------
    raw : bytes
        A WAV clip at any sample rate (espeak-ng's native output).

    Returns
    -------
    bytes
        A 44.1 kHz mono signed-16-bit WAV clip.

    Raises
    ------
    TtsUnavailableError
        ``ffmpeg`` is not installed on the device.
    TtsSynthesisError
        ffmpeg could not transcode the audio.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise TtsUnavailableError("ffmpeg is not installed on the device (apt install ffmpeg)")
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
                str(_OUTPUT_RATE_HZ),
                "-ac",
                "1",
                "-sample_fmt",
                "s16",
                "-f",
                "wav",
                "pipe:1",
            ],
            input=raw,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise TtsSynthesisError("audio encode timed out") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip().splitlines()
        raise TtsSynthesisError(
            f"could not encode speech audio: {detail[-1] if detail else 'unknown ffmpeg error'}"
        )
    if len(proc.stdout) == 0:
        raise TtsSynthesisError("speech audio encoded to no samples")
    return proc.stdout
