"""Tests for the text-to-speech engine module (nomothetic.tts).

The espeak-ng engine is exercised with ``shutil.which`` and ``subprocess.run``
patched, so no synthesiser, audio codec, or Pi hardware is needed. The
wake-word listener integration is covered in test_wake.py.
"""

from unittest.mock import MagicMock, patch

import pytest

from nomothetic import tts
from nomothetic.tts import (
    EspeakTtsEngine,
    TtsSynthesisError,
    TtsUnavailableError,
    _normalise_wav,
    _resolve_rate,
)

# ============================================================================
# Helpers
# ============================================================================


def _which(**paths):
    """A ``shutil.which`` stand-in resolving only the named binaries."""
    table = {
        "espeak-ng": paths.get("espeak_ng", "/usr/bin/espeak-ng"),
        "espeak": paths.get("espeak", "/usr/bin/espeak"),
        "ffmpeg": paths.get("ffmpeg", "/usr/bin/ffmpeg"),
    }
    return lambda name: table.get(name)


def _proc(returncode=0, stdout=b"", stderr=b""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


# ============================================================================
# Rate resolution
# ============================================================================


def test_resolve_rate_default(monkeypatch):
    monkeypatch.delenv("NOMON_TTS_RATE_WPM", raising=False)
    assert _resolve_rate(None) == 160


def test_resolve_rate_from_env(monkeypatch):
    monkeypatch.setenv("NOMON_TTS_RATE_WPM", "200")
    assert _resolve_rate(None) == 200


def test_resolve_rate_junk_env_falls_back(monkeypatch):
    monkeypatch.setenv("NOMON_TTS_RATE_WPM", "quick")
    assert _resolve_rate(None) == 160


def test_resolve_rate_clamps(monkeypatch):
    monkeypatch.delenv("NOMON_TTS_RATE_WPM", raising=False)
    assert _resolve_rate(10) == 80
    assert _resolve_rate(9999) == 450
    assert _resolve_rate(300) == 300


# ============================================================================
# _normalise_wav (ffmpeg transcode)
# ============================================================================


def test_normalise_requires_ffmpeg():
    with patch.object(tts.shutil, "which", return_value=None):
        with pytest.raises(TtsUnavailableError, match="ffmpeg"):
            _normalise_wav(b"wav")


def test_normalise_requests_44100_mono_s16_wav():
    with patch.object(tts.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(tts.subprocess, "run", return_value=_proc(stdout=b"out")) as run:
            assert _normalise_wav(b"raw") == b"out"
    argv = run.call_args.args[0]
    assert "44100" in argv and "-ac" in argv and "1" in argv
    assert argv[argv.index("-sample_fmt") + 1] == "s16"
    assert argv[argv.index("-f") + 1] == "wav"
    assert run.call_args.kwargs["input"] == b"raw"


def test_normalise_raises_on_ffmpeg_error():
    with patch.object(tts.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(tts.subprocess, "run", return_value=_proc(returncode=1, stderr=b"boom")):
            with pytest.raises(TtsSynthesisError, match="boom"):
                _normalise_wav(b"raw")


def test_normalise_raises_on_empty_output():
    with patch.object(tts.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(tts.subprocess, "run", return_value=_proc(stdout=b"")):
            with pytest.raises(TtsSynthesisError, match="no samples"):
                _normalise_wav(b"raw")


# ============================================================================
# EspeakTtsEngine.synthesize
# ============================================================================


def test_engine_name():
    assert EspeakTtsEngine().name == "espeak-ng"


def test_voice_from_env(monkeypatch):
    monkeypatch.setenv("NOMON_TTS_VOICE", "en-us")
    assert EspeakTtsEngine()._voice == "en-us"


def test_synthesize_empty_text_returns_empty():
    with patch.object(tts.subprocess, "run") as run:
        assert EspeakTtsEngine().synthesize("   ") == b""
    run.assert_not_called()


def test_synthesize_pipes_espeak_into_ffmpeg(monkeypatch):
    monkeypatch.delenv("NOMON_TTS_VOICE", raising=False)
    monkeypatch.delenv("NOMON_TTS_RATE_WPM", raising=False)
    espeak_proc = _proc(stdout=b"raw-wav")
    ffmpeg_proc = _proc(stdout=b"final-wav")
    with patch.object(tts.shutil, "which", side_effect=_which()):
        with patch.object(tts.subprocess, "run", side_effect=[espeak_proc, ffmpeg_proc]) as run:
            out = EspeakTtsEngine().synthesize("  drive forward  ")
    assert out == b"final-wav"
    # First call: espeak-ng, text passed as a single stripped argv element.
    espeak_argv = run.call_args_list[0].args[0]
    assert espeak_argv[0] == "/usr/bin/espeak-ng"
    assert "--stdout" in espeak_argv
    assert espeak_argv[espeak_argv.index("-v") + 1] == "en"
    assert espeak_argv[espeak_argv.index("-s") + 1] == "160"
    assert espeak_argv[-1] == "drive forward"
    # Second call: ffmpeg fed espeak's stdout.
    assert run.call_args_list[1].kwargs["input"] == b"raw-wav"


def test_synthesize_falls_back_to_espeak_binary(monkeypatch):
    # espeak-ng absent, legacy espeak present.
    which = _which(espeak_ng=None)
    with patch.object(tts.shutil, "which", side_effect=which):
        with patch.object(
            tts.subprocess, "run", side_effect=[_proc(stdout=b"raw"), _proc(stdout=b"x")]
        ):
            out = EspeakTtsEngine().synthesize("hi")
    assert out == b"x"


def test_synthesize_requires_espeak():
    with patch.object(tts.shutil, "which", side_effect=_which(espeak_ng=None, espeak=None)):
        with pytest.raises(TtsUnavailableError, match="espeak-ng"):
            EspeakTtsEngine().synthesize("hi")


def test_synthesize_raises_on_espeak_error():
    with patch.object(tts.shutil, "which", side_effect=_which()):
        with patch.object(tts.subprocess, "run", return_value=_proc(returncode=1, stderr=b"nope")):
            with pytest.raises(TtsSynthesisError, match="nope"):
                EspeakTtsEngine().synthesize("hi")


def test_synthesize_raises_on_empty_espeak_output():
    with patch.object(tts.shutil, "which", side_effect=_which()):
        with patch.object(tts.subprocess, "run", return_value=_proc(stdout=b"")):
            with pytest.raises(TtsSynthesisError, match="no audio"):
                EspeakTtsEngine().synthesize("hi")


def test_synthesize_timeout_raises():
    import subprocess

    with patch.object(tts.shutil, "which", side_effect=_which()):
        with patch.object(
            tts.subprocess, "run", side_effect=subprocess.TimeoutExpired("espeak-ng", 15.0)
        ):
            with pytest.raises(TtsSynthesisError, match="timed out"):
                EspeakTtsEngine().synthesize("hi")
