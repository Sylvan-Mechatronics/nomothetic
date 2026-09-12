"""Tests for the nomothetic.audio module.

These tests do not require real hardware or PyAudio — pyaudio is mocked.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nomothetic.audio import (
    AudioPlayer,
    AudioRecorder,
    list_audio_files,
    resolve_input_device,
    resolve_output_device,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: PortAudio device-info dict shaped like the robot's USB PCM2902 codec.
_USB_DEVICE = {
    "name": "USB PnP Sound Device: Audio (hw:1,0)",
    "maxInputChannels": 1,
    "maxOutputChannels": 2,
}


def _write_dummy_wav(path: Path, duration_s: float = 0.1, rate: int = 44100) -> None:
    """Write a minimal valid WAV file for testing."""
    n_frames = int(rate * duration_s)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{n_frames}h", *([0] * n_frames)))


def _pa_instance(devices=None, default_input_index=None, default_output_index=None):
    """Configure a ``pyaudio.PyAudio()`` instance mock with device enumeration."""
    if devices is None:
        devices = [_USB_DEVICE]
        default_input_index = 0
        default_output_index = 0
    inst = MagicMock()
    inst.get_device_count.return_value = len(devices)
    inst.get_device_info_by_index.side_effect = lambda i: devices[i]
    if default_input_index is None:
        inst.get_default_input_device_info.side_effect = OSError("no default input device")
    else:
        inst.get_default_input_device_info.return_value = {
            "index": default_input_index,
            **devices[default_input_index],
        }
    if default_output_index is None:
        inst.get_default_output_device_info.side_effect = OSError("no default output device")
    else:
        inst.get_default_output_device_info.return_value = {
            "index": default_output_index,
            **devices[default_output_index],
        }
    return inst


def _recording_pyaudio(devices=None, default_input_index=None):
    """Return a ``pyaudio.PyAudio`` class mock whose instance records silence."""
    if devices is None:
        inst = _pa_instance()
    else:
        inst = _pa_instance(devices, default_input_index=default_input_index)
    mock_stream = MagicMock()
    mock_stream.read.return_value = b"\x00" * 2048
    inst.open.return_value = mock_stream
    return MagicMock(return_value=inst)


# ---------------------------------------------------------------------------
# list_audio_files()
# ---------------------------------------------------------------------------


def test_list_audio_files_empty_dir(tmp_path):
    """`list_audio_files` returns [] for an empty directory."""
    assert list_audio_files(tmp_path) == []


def test_list_audio_files_missing_dir(tmp_path):
    """`list_audio_files` returns [] when the directory does not exist."""
    assert list_audio_files(tmp_path / "nonexistent") == []


def test_list_audio_files_returns_sorted_wav_names(tmp_path):
    """`list_audio_files` returns sorted WAV basenames."""
    for name in ("c.wav", "a.wav", "b.wav"):
        (tmp_path / name).touch()
    (tmp_path / "image.jpg").touch()  # should be excluded
    result = list_audio_files(tmp_path)
    assert result == ["a.wav", "b.wav", "c.wav"]


# ---------------------------------------------------------------------------
# Device resolution (regression: blind index 2 segfaulted libportaudio)
# ---------------------------------------------------------------------------


class TestDeviceResolution:
    def test_input_prefers_named_usb_device(self):
        """The USB mic wins by name; ALSA's virtual 'default' (which advertises
        capture it cannot deliver and segfaults libportaudio) is never picked."""
        pa = _pa_instance(
            [
                {"name": "vc4-hdmi", "maxInputChannels": 0, "maxOutputChannels": 2},
                _USB_DEVICE,
                {"name": "default", "maxInputChannels": 32, "maxOutputChannels": 32},
            ]
        )
        assert resolve_input_device(pa) == 1

    def test_input_falls_back_to_default_input(self):
        pa = _pa_instance(
            [{"name": "fancy array mic", "maxInputChannels": 2, "maxOutputChannels": 0}],
            default_input_index=0,
        )
        assert resolve_input_device(pa) == 0

    def test_input_none_when_no_input_devices(self):
        pa = _pa_instance([{"name": "hdmi out", "maxInputChannels": 0, "maxOutputChannels": 2}])
        assert resolve_input_device(pa) is None

    def test_explicit_override_skips_enumeration(self):
        pa = MagicMock()
        assert resolve_input_device(pa, explicit_index=7) == 7
        assert resolve_output_device(pa, explicit_index=3) == 3
        pa.get_device_count.assert_not_called()

    def test_custom_name_match(self):
        pa = _pa_instance(
            [
                _USB_DEVICE,
                {"name": "Blue Yeti Nano", "maxInputChannels": 2, "maxOutputChannels": 0},
            ]
        )
        assert resolve_input_device(pa, name_match="yeti") == 1

    def test_output_prefers_named_usb_device(self):
        pa = _pa_instance(
            [
                {"name": "vc4-hdmi", "maxInputChannels": 0, "maxOutputChannels": 2},
                _USB_DEVICE,
            ],
            default_output_index=0,
        )
        assert resolve_output_device(pa) == 1

    def test_output_falls_back_to_default_output(self):
        pa = _pa_instance(
            [{"name": "vc4-hdmi", "maxInputChannels": 0, "maxOutputChannels": 2}],
            default_output_index=0,
        )
        assert resolve_output_device(pa) == 0

    def test_output_none_when_no_output_devices(self):
        pa = _pa_instance([{"name": "array mic", "maxInputChannels": 2, "maxOutputChannels": 0}])
        assert resolve_output_device(pa) is None


# ---------------------------------------------------------------------------
# AudioRecorder
# ---------------------------------------------------------------------------


class TestAudioRecorder:
    def test_is_not_recording_initially(self, tmp_path):
        recorder = AudioRecorder(audio_dir=tmp_path)
        assert recorder.is_recording is False
        assert recorder.current_file is None

    def test_no_env_means_autodetect_by_name(self, monkeypatch, tmp_path):
        """ALSA-style numeric defaults are gone (they segfaulted libportaudio on
        the Pi); unset index means auto-detect by name."""
        monkeypatch.delenv("NOMON_AUDIO_INPUT_INDEX", raising=False)
        monkeypatch.delenv("NOMON_AUDIO_INPUT_NAME", raising=False)
        recorder = AudioRecorder(audio_dir=tmp_path)
        assert recorder._input_device_index is None
        assert recorder._input_name_match == "usb"

    def test_env_explicit_input_index_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NOMON_AUDIO_INPUT_INDEX", "5")
        assert AudioRecorder(audio_dir=tmp_path)._input_device_index == 5

    def test_env_junk_input_index_means_autodetect(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NOMON_AUDIO_INPUT_INDEX", "card2")
        assert AudioRecorder(audio_dir=tmp_path)._input_device_index is None

    def test_env_input_name_match(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NOMON_AUDIO_INPUT_NAME", " PCM2902 ")
        assert AudioRecorder(audio_dir=tmp_path)._input_name_match == "pcm2902"

    def test_record_opens_named_usb_device(self, tmp_path):
        """End-to-end: with no index configured the USB device gets opened."""
        MockPyAudio = _recording_pyaudio(
            devices=[
                {"name": "vc4-hdmi", "maxInputChannels": 0, "maxOutputChannels": 2},
                _USB_DEVICE,
                {"name": "default", "maxInputChannels": 32, "maxOutputChannels": 32},
            ]
        )
        with (
            patch("nomothetic.audio._PYAUDIO_AVAILABLE", True),
            patch("nomothetic.audio.pyaudio") as mock_pa_module,
        ):
            mock_pa_module.PyAudio = MockPyAudio
            mock_pa_module.paInt16 = 8

            recorder = AudioRecorder(audio_dir=tmp_path)
            recorder.start("clip.wav")
            recorder.stop()  # joins the recording thread
        assert MockPyAudio.return_value.open.call_args.kwargs["input_device_index"] == 1

    def test_record_skips_wav_when_no_capture_device(self, tmp_path):
        """No usable capture device logs an error instead of crashing the API."""
        MockPyAudio = MagicMock(
            return_value=_pa_instance(
                [{"name": "hdmi out", "maxInputChannels": 0, "maxOutputChannels": 2}]
            )
        )
        with (
            patch("nomothetic.audio._PYAUDIO_AVAILABLE", True),
            patch("nomothetic.audio.pyaudio") as mock_pa_module,
        ):
            mock_pa_module.PyAudio = MockPyAudio
            mock_pa_module.paInt16 = 8

            recorder = AudioRecorder(audio_dir=tmp_path)
            out = recorder.start("clip.wav")
            recorder.stop()
        MockPyAudio.return_value.open.assert_not_called()
        assert not Path(out).exists()

    def test_start_raises_when_already_recording(self, tmp_path):
        """Starting a second recording raises RuntimeError."""
        with (
            patch("nomothetic.audio._PYAUDIO_AVAILABLE", True),
            patch("nomothetic.audio.pyaudio") as mock_pa_module,
        ):
            mock_pa_module.PyAudio = _recording_pyaudio()
            mock_pa_module.paInt16 = 8

            recorder = AudioRecorder(audio_dir=tmp_path)
            recorder.start()
            assert recorder.is_recording
            with pytest.raises(RuntimeError, match="already in progress"):
                recorder.start()
            recorder.stop()

    def test_start_without_pyaudio_raises(self, tmp_path):
        """RuntimeError raised when pyaudio is not installed."""
        with patch("nomothetic.audio._PYAUDIO_AVAILABLE", False):
            recorder = AudioRecorder(audio_dir=tmp_path)
            with pytest.raises(RuntimeError, match="pyaudio"):
                recorder.start()

    def test_start_rejects_absolute_path(self, tmp_path):
        """ValueError raised when filename is an absolute path."""
        with patch("nomothetic.audio._PYAUDIO_AVAILABLE", True):
            recorder = AudioRecorder(audio_dir=tmp_path)
            with pytest.raises(ValueError, match="absolute"):
                recorder.start("/etc/passwd")

    def test_start_rejects_path_traversal(self, tmp_path):
        """ValueError raised when filename contains path separators."""
        with patch("nomothetic.audio._PYAUDIO_AVAILABLE", True):
            recorder = AudioRecorder(audio_dir=tmp_path)
            with pytest.raises(ValueError, match="separator"):
                recorder.start("../escape.wav")

    def test_start_rejects_dotfile(self, tmp_path):
        """ValueError raised when filename starts with a dot."""
        with patch("nomothetic.audio._PYAUDIO_AVAILABLE", True):
            recorder = AudioRecorder(audio_dir=tmp_path)
            with pytest.raises(ValueError, match=r"\.'"):
                recorder.start(".hidden.wav")

    def test_stop_with_no_recording_returns_none(self, tmp_path):
        recorder = AudioRecorder(audio_dir=tmp_path)
        assert recorder.stop() is None

    def test_start_generates_timestamped_filename(self, tmp_path):
        """When no filename is given, a timestamped name is generated."""
        with (
            patch("nomothetic.audio._PYAUDIO_AVAILABLE", True),
            patch("nomothetic.audio.pyaudio") as mock_pa_module,
        ):
            mock_pa_module.PyAudio = _recording_pyaudio()
            mock_pa_module.paInt16 = 8

            recorder = AudioRecorder(audio_dir=tmp_path)
            path = recorder.start()
            assert "recording_" in path
            assert path.endswith(".wav")
            recorder.stop()

    def test_start_with_custom_filename(self, tmp_path):
        """Explicit filename is used as-is."""
        with (
            patch("nomothetic.audio._PYAUDIO_AVAILABLE", True),
            patch("nomothetic.audio.pyaudio") as mock_pa_module,
        ):
            mock_pa_module.PyAudio = _recording_pyaudio()
            mock_pa_module.paInt16 = 8

            recorder = AudioRecorder(audio_dir=tmp_path)
            path = recorder.start("test_clip.wav")
            assert path == str(tmp_path / "test_clip.wav")
            recorder.stop()

    def test_stop_returns_output_path(self, tmp_path):
        """stop() returns the path of the recorded file."""
        with (
            patch("nomothetic.audio._PYAUDIO_AVAILABLE", True),
            patch("nomothetic.audio.pyaudio") as mock_pa_module,
        ):
            mock_pa_module.PyAudio = _recording_pyaudio()
            mock_pa_module.paInt16 = 8

            recorder = AudioRecorder(audio_dir=tmp_path)
            out = recorder.start("out.wav")
            result = recorder.stop()
            assert result == out
            assert recorder.is_recording is False


# ---------------------------------------------------------------------------
# AudioPlayer
# ---------------------------------------------------------------------------


class TestAudioPlayer:
    def test_is_not_playing_initially(self, tmp_path):
        player = AudioPlayer(audio_dir=tmp_path)
        assert player.is_playing is False
        assert player.current_file is None

    def test_no_env_means_autodetect_by_name(self, monkeypatch, tmp_path):
        monkeypatch.delenv("NOMON_AUDIO_OUTPUT_INDEX", raising=False)
        monkeypatch.delenv("NOMON_AUDIO_OUTPUT_NAME", raising=False)
        player = AudioPlayer(audio_dir=tmp_path)
        assert player._output_device_index is None
        assert player._output_name_match == "usb"

    def test_env_explicit_output_index_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NOMON_AUDIO_OUTPUT_INDEX", "4")
        assert AudioPlayer(audio_dir=tmp_path)._output_device_index == 4

    def test_play_without_pyaudio_raises(self, tmp_path):
        _write_dummy_wav(tmp_path / "clip.wav")
        with patch("nomothetic.audio._PYAUDIO_AVAILABLE", False):
            player = AudioPlayer(audio_dir=tmp_path)
            with pytest.raises(RuntimeError, match="pyaudio"):
                player.play("clip.wav")

    def test_play_missing_file_raises(self, tmp_path):
        with patch("nomothetic.audio._PYAUDIO_AVAILABLE", True):
            player = AudioPlayer(audio_dir=tmp_path)
            with pytest.raises(FileNotFoundError):
                player.play("missing.wav")

    def test_play_opens_named_usb_output_device(self, tmp_path):
        """End-to-end: playback lands on the USB codec, not the silent HDMI out."""
        _write_dummy_wav(tmp_path / "clip.wav")
        inst = _pa_instance(
            [
                {"name": "vc4-hdmi", "maxInputChannels": 0, "maxOutputChannels": 2},
                _USB_DEVICE,
            ],
            default_output_index=0,
        )
        inst.open.return_value = MagicMock()
        with (
            patch("nomothetic.audio._PYAUDIO_AVAILABLE", True),
            patch("nomothetic.audio.pyaudio") as mock_pa_module,
        ):
            mock_pa_module.PyAudio = MagicMock(return_value=inst)

            player = AudioPlayer(audio_dir=tmp_path)
            player.play("clip.wav")
            if player._thread is not None:
                player._thread.join(timeout=2.0)
        assert inst.open.call_args.kwargs["output_device_index"] == 1

    def test_play_while_already_playing_raises(self, tmp_path):
        import threading

        _write_dummy_wav(tmp_path / "a.wav")
        _write_dummy_wav(tmp_path / "b.wav")

        # Use events so the background play thread is held inside stream.write()
        # while we verify that a second play() call raises RuntimeError.
        # Without this, on fast CI runners the thread can finish and clear
        # _playing before the second play() call, causing a spurious pass.
        write_started = threading.Event()
        unblock_write = threading.Event()

        def blocking_write(data):
            write_started.set()
            unblock_write.wait()

        mock_stream = MagicMock()
        mock_stream.write.side_effect = blocking_write
        inst = _pa_instance()
        inst.open.return_value = mock_stream
        MockPyAudio = MagicMock(return_value=inst)

        with (
            patch("nomothetic.audio._PYAUDIO_AVAILABLE", True),
            patch("nomothetic.audio.pyaudio") as mock_pa_module,
        ):
            mock_pa_module.PyAudio = MockPyAudio
            mock_pa_module.paFloat32 = 1

            player = AudioPlayer(audio_dir=tmp_path)
            player.play("a.wav")
            # Wait until background thread is blocked inside write(), so
            # _playing is guaranteed to still be True.
            assert write_started.wait(timeout=5.0), "play thread did not start"
            with pytest.raises(RuntimeError, match="already in progress"):
                player.play("b.wav")
            unblock_write.set()
            player.stop()

    def test_stop_when_not_playing_is_safe(self, tmp_path):
        player = AudioPlayer(audio_dir=tmp_path)
        player.stop()  # should not raise

    def test_play_resolves_bare_filename_against_audio_dir(self, tmp_path):
        """A bare filename (no directory) is resolved against audio_dir."""
        _write_dummy_wav(tmp_path / "clip.wav")

        mock_stream = MagicMock()
        inst = _pa_instance()
        inst.open.return_value = mock_stream
        MockPyAudio = MagicMock(return_value=inst)
        mock_stream.read.return_value = b""

        with (
            patch("nomothetic.audio._PYAUDIO_AVAILABLE", True),
            patch("nomothetic.audio.pyaudio") as mock_pa_module,
            patch("nomothetic.audio.wave") as mock_wave,
        ):
            mock_pa_module.PyAudio = MockPyAudio
            mock_wave_file = MagicMock()
            mock_wave_file.__enter__ = lambda s: s
            mock_wave_file.__exit__ = MagicMock(return_value=False)
            mock_wave_file.getnchannels.return_value = 1
            mock_wave_file.getsampwidth.return_value = 2
            mock_wave_file.getframerate.return_value = 44100
            mock_wave_file.readframes.return_value = b""
            mock_wave.open.return_value = mock_wave_file

            player = AudioPlayer(audio_dir=tmp_path)
            player.play("clip.wav")
            # Wait for background thread to finish, then verify wave.open was
            # called with the fully-resolved path (not just the bare basename).
            if player._thread is not None:
                player._thread.join(timeout=2.0)
            mock_wave.open.assert_called_once_with(str(tmp_path / "clip.wav"), "rb")
