"""Tests for the speech-to-text engine module (nomothetic.stt).

The Vosk engine is exercised with the ``vosk`` package mocked out and the
ffmpeg normalisation helper patched, so no model download, audio codec, or
Pi hardware is needed. The HTTP endpoint is covered in test_ai_routes.py.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from nomothetic import stt
from nomothetic.stt import (
    SttResult,
    SttTranscriptionError,
    SttUnavailableError,
    VoskSttEngine,
    _to_pcm,
)

# ============================================================================
# Fakes
# ============================================================================


def _fake_vosk(final_result: str = '{"text": "drive forward"}') -> MagicMock:
    """Build a mock of the ``vosk`` module with a canned recognition result."""
    fake = MagicMock()
    recognizer = MagicMock()
    recognizer.FinalResult.return_value = final_result
    fake.KaldiRecognizer.return_value = recognizer
    return fake


@pytest.fixture
def model_dir(tmp_path):
    """An existing directory standing in for an unpacked Vosk model."""
    path = tmp_path / "vosk-model-small-en-us-0.15"
    path.mkdir()
    return str(path)


# ============================================================================
# _to_pcm (ffmpeg normalisation)
# ============================================================================


def test_to_pcm_requires_ffmpeg():
    with patch.object(stt.shutil, "which", return_value=None):
        with pytest.raises(SttUnavailableError, match="ffmpeg"):
            _to_pcm(b"audio")


def test_to_pcm_success_requests_16k_mono_s16le():
    proc = MagicMock(returncode=0, stdout=b"pcm-bytes", stderr=b"")
    with patch.object(stt.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(stt.subprocess, "run", return_value=proc) as run:
            assert _to_pcm(b"audio") == b"pcm-bytes"
    argv = run.call_args.args[0]
    assert run.call_args.kwargs["input"] == b"audio"
    for expected in ("-ar", "16000", "-ac", "1", "-f", "s16le"):
        assert expected in argv


def test_to_pcm_decode_failure_raises_transcription_error():
    proc = MagicMock(returncode=1, stdout=b"", stderr=b"pipe:0: Invalid data found")
    with patch.object(stt.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(stt.subprocess, "run", return_value=proc):
            with pytest.raises(SttTranscriptionError, match="could not decode"):
                _to_pcm(b"not-audio")


def test_to_pcm_empty_output_raises_transcription_error():
    proc = MagicMock(returncode=0, stdout=b"", stderr=b"")
    with patch.object(stt.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(stt.subprocess, "run", return_value=proc):
            with pytest.raises(SttTranscriptionError, match="no samples"):
                _to_pcm(b"audio")


def test_to_pcm_timeout_raises_transcription_error():
    with patch.object(stt.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(
            stt.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30),
        ):
            with pytest.raises(SttTranscriptionError, match="timed out"):
                _to_pcm(b"audio")


# ============================================================================
# VoskSttEngine
# ============================================================================


def test_transcribe_without_vosk_raises_unavailable(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    with patch.object(stt, "vosk", None):
        with pytest.raises(SttUnavailableError, match="not installed"):
            engine.transcribe(b"audio", "audio/mp4")


def test_transcribe_without_model_dir_raises_unavailable(tmp_path):
    missing = str(tmp_path / "no-such-model")
    engine = VoskSttEngine(model_path=missing)
    with patch.object(stt, "vosk", _fake_vosk()):
        with pytest.raises(SttUnavailableError, match="no-such-model"):
            engine.transcribe(b"audio", "audio/mp4")


def test_transcribe_success(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    fake = _fake_vosk('{"text": " drive forward "}')
    with patch.object(stt, "vosk", fake):
        with patch.object(stt, "_to_pcm", return_value=b"\x00\x01" * 100) as to_pcm:
            result = engine.transcribe(b"m4a-bytes", "audio/mp4")
    assert result == SttResult(text="drive forward", engine="vosk")
    to_pcm.assert_called_once_with(b"m4a-bytes")
    fake.Model.assert_called_once_with(model_dir)
    fake.KaldiRecognizer.assert_called_once_with(fake.Model.return_value, 16000)


def test_transcribe_silence_returns_empty_text(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    with patch.object(stt, "vosk", _fake_vosk('{"text": ""}')):
        with patch.object(stt, "_to_pcm", return_value=b"\x00\x00"):
            result = engine.transcribe(b"audio", "audio/webm")
    assert result.text == ""


def test_model_loaded_once_across_calls(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    fake = _fake_vosk()
    with patch.object(stt, "vosk", fake):
        with patch.object(stt, "_to_pcm", return_value=b"\x00\x01"):
            engine.transcribe(b"one", "audio/mp4")
            engine.transcribe(b"two", "audio/mp4")
    assert fake.Model.call_count == 1


def test_transcribe_feeds_pcm_in_chunks(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    fake = _fake_vosk()
    pcm = b"\x00" * (stt._PCM_CHUNK_BYTES * 2 + 1)
    with patch.object(stt, "vosk", fake):
        with patch.object(stt, "_to_pcm", return_value=pcm):
            engine.transcribe(b"audio", "audio/mp4")
    recognizer = fake.KaldiRecognizer.return_value
    assert recognizer.AcceptWaveform.call_count == 3
    fed = b"".join(call.args[0] for call in recognizer.AcceptWaveform.call_args_list)
    assert fed == pcm


def test_unload_when_nothing_loaded_returns_false(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    assert engine.unload() is False


def test_unload_releases_model_and_next_use_reloads_it(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    fake = _fake_vosk()
    with patch.object(stt, "vosk", fake):
        with patch.object(stt, "_to_pcm", return_value=b"\x00\x01"):
            engine.transcribe(b"one", "audio/mp4")
            assert fake.Model.call_count == 1
            assert engine.unload() is True
            engine.transcribe(b"two", "audio/mp4")
    assert fake.Model.call_count == 2


def test_unload_is_idempotent(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    fake = _fake_vosk()
    with patch.object(stt, "vosk", fake):
        with patch.object(stt, "_to_pcm", return_value=b"\x00\x01"):
            engine.transcribe(b"audio", "audio/mp4")
    assert engine.unload() is True
    assert engine.unload() is False


def test_model_path_from_env(monkeypatch, model_dir):
    monkeypatch.setenv("NOMON_STT_MODEL_PATH", model_dir)
    engine = VoskSttEngine()
    fake = _fake_vosk()
    with patch.object(stt, "vosk", fake):
        with patch.object(stt, "_to_pcm", return_value=b"\x00\x01"):
            engine.transcribe(b"audio", "audio/mp4")
    fake.Model.assert_called_once_with(model_dir)


# ============================================================================
# create_recognizer (wake-word seam, ADR-021)
# ============================================================================


def test_create_recognizer_with_grammar(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    fake = _fake_vosk()
    with patch.object(stt, "vosk", fake):
        recognizer = engine.create_recognizer(16000, '["hey nomon", "[unk]"]')
    assert recognizer is fake.KaldiRecognizer.return_value
    fake.KaldiRecognizer.assert_called_once_with(
        fake.Model.return_value, 16000, '["hey nomon", "[unk]"]'
    )


def test_create_recognizer_without_grammar_uses_two_arg_form(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    fake = _fake_vosk()
    with patch.object(stt, "vosk", fake):
        engine.create_recognizer()
    fake.KaldiRecognizer.assert_called_once_with(fake.Model.return_value, 16000)


def test_create_recognizer_shares_one_model_with_transcribe(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    fake = _fake_vosk()
    with patch.object(stt, "vosk", fake):
        with patch.object(stt, "_to_pcm", return_value=b"\x00\x01"):
            engine.create_recognizer(16000, '["hey nomon", "[unk]"]')
            engine.transcribe(b"audio", "audio/mp4")
    assert fake.Model.call_count == 1


def test_create_recognizer_without_vosk_raises_unavailable(model_dir):
    engine = VoskSttEngine(model_path=model_dir)
    with patch.object(stt, "vosk", None):
        with pytest.raises(SttUnavailableError, match="not installed"):
            engine.create_recognizer()


def test_create_recognizer_missing_model_dir_raises_unavailable(tmp_path):
    engine = VoskSttEngine(model_path=str(tmp_path / "no-such-model"))
    with patch.object(stt, "vosk", _fake_vosk()):
        with pytest.raises(SttUnavailableError, match="no-such-model"):
            engine.create_recognizer()
