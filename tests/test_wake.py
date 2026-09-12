"""Tests for the wake-word listener (nomothetic.wake), ADR-021.

No hardware, vosk, or pyaudio needed: recognizers are scripted fakes, the
microphone is a fake PyAudio stream, and AI dispatches land on a real event
loop running in a background thread (mirroring the app's lifespan loop).
The HTTP endpoints are covered in test_wake_routes.py.
"""

from __future__ import annotations

import asyncio
import math
import struct
import threading
import time
import wave
from unittest.mock import MagicMock

import pytest

from nomothetic import wake
from nomothetic.ai_command import AiProviderError
from nomothetic.stt import SttUnavailableError
from nomothetic.wake import (
    WakeWordListener,
    _build_grammar,
    _resample_to_16k,
    _Resampler,
    _rms,
    _trim_history,
    parse_variants,
    synthesize_chimes,
)


def _tone_pcm(freq_hz: float, rate_hz: int, n_samples: int, amp: int = 10000) -> bytes:
    """A mono 16-bit sine tone as PCM bytes."""
    return struct.pack(
        f"<{n_samples}h",
        *(int(amp * math.sin(2 * math.pi * freq_hz * i / rate_hz)) for i in range(n_samples)),
    )


def _unpack_pcm(pcm: bytes) -> list[int]:
    """Unpack 16-bit mono PCM bytes to a list of ints."""
    return list(struct.unpack(f"<{len(pcm) // 2}h", pcm))


# 100 ms of 16 kHz mono int16.
LOUD_FRAME = struct.pack("<1600h", *([12000] * 1600))
SILENT_FRAME = bytes(3200)


def wait_for(predicate, timeout_s: float = 3.0) -> bool:
    """Poll *predicate* until true or *timeout_s* elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


# ============================================================================
# Fakes
# ============================================================================


class FakeRecognizer:
    """Scripted recognizer: each fed frame pops the next scripted outcome."""

    def __init__(self, script=None):
        #: items: ("final", text) or ("partial", text); exhausted -> silence.
        self.script = list(script or [])
        self.accepted: list[bytes] = []
        self.reset_count = 0
        self._last_final = ""
        self._last_partial = ""

    def AcceptWaveform(self, data):  # noqa: N802 - vosk API shape
        self.accepted.append(data)
        self._last_partial = ""
        if self.script:
            kind, text = self.script.pop(0)
            if kind == "final":
                self._last_final = text
                return True
            self._last_partial = text
        return False

    def Result(self):  # noqa: N802 - vosk API shape
        return f'{{"text": "{self._last_final}"}}'

    def PartialResult(self):  # noqa: N802 - vosk API shape
        return f'{{"partial": "{self._last_partial}"}}'

    def FinalResult(self):  # noqa: N802 - vosk API shape
        return '{"text": ""}'

    def Reset(self):  # noqa: N802 - vosk API shape
        self.reset_count += 1


class FakeEngine:
    """RecognizerProvider returning a wake (grammar) and command recognizer."""

    def __init__(self, wake_recognizer, command_recognizer, fail=None):
        self.wake_recognizer = wake_recognizer
        self.command_recognizer = command_recognizer
        self.fail = fail
        self.calls: list[tuple[float, str | None]] = []
        self.unloaded = False

    def create_recognizer(self, sample_rate_hz=16000, grammar_json=None):
        if self.fail is not None:
            raise self.fail
        self.calls.append((sample_rate_hz, grammar_json))
        return self.wake_recognizer if grammar_json is not None else self.command_recognizer

    def unload(self) -> bool:
        self.unloaded = True
        return True


class FakeStream:
    """Fake PyAudio input stream feeding scripted frames, then silence."""

    def __init__(self, frames=None):
        self.frames = list(frames or [])
        self.closed = False

    def read(self, n, exception_on_overflow=False):
        if self.frames:
            item = self.frames.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        time.sleep(0.005)
        return SILENT_FRAME

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class FakeAiService:
    """CommandRunner recording dispatched conversations."""

    def __init__(self, reply="Done.", fail=None):
        self.reply = reply
        self.fail = fail
        self.calls: list[list[dict]] = []

    async def run_command(self, messages, api_key):
        self.calls.append([dict(m) for m in messages])
        if self.fail is not None:
            raise self.fail
        return {"reply": self.reply, "actions": [], "model": "m", "stop_reason": "end_turn"}


class FakeKeyStore:
    """KeyResolver with a settable key."""

    def __init__(self, key="sk-ant-test-key"):
        self.key = key

    def resolve(self):
        return (self.key, "stored") if self.key else None


class FakeTts:
    """SpeechSynthesizer recording synthesised text; returns canned WAV bytes."""

    def __init__(self, wav: bytes = b"", fail: Exception | None = None):
        self.wav = wav
        self.fail = fail
        self.calls: list[str] = []

    def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        if self.fail is not None:
            raise self.fail
        return self.wav


class RecordingListener(WakeWordListener):
    """Listener with chime and speech playback replaced by in-memory records."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.chimes: list[str] = []
        self.spoken: list[str] = []

    def _play_chime(self, name: str) -> None:
        self.chimes.append(name)

    def _speak(self, text: str) -> None:
        self.spoken.append(text)


# ============================================================================
# Fixtures & builders
# ============================================================================


@pytest.fixture
def bg_loop():
    """A running event loop in a background thread (stands in for the app loop)."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5.0)
    loop.close()


def _patch_pyaudio(monkeypatch, mic_stream=None, open_error=None):
    """Patch nomothetic.wake's pyaudio with a fake; returns the PyAudio mock."""
    module = MagicMock()
    module.paInt16 = 8
    pa = MagicMock()
    output_streams: list[MagicMock] = []

    def open_stream(**kwargs):
        if open_error is not None:
            raise open_error
        if kwargs.get("input"):
            return mic_stream if mic_stream is not None else FakeStream()
        stream = MagicMock()
        output_streams.append(stream)
        return stream

    pa.open.side_effect = open_stream
    module.PyAudio.return_value = pa
    monkeypatch.setattr(wake, "pyaudio", module)
    monkeypatch.setattr(wake, "_PYAUDIO_AVAILABLE", True)
    pa.output_streams = output_streams
    # Exposed so tests can assert the host itself is built once, not per
    # stream-open/chime/TTS clip (module.PyAudio.return_value is fixed to
    # `pa` regardless of call count, so this is purely for introspection).
    pa.host_factory = module.PyAudio
    return pa


def _make_listener(
    engine,
    tmp_path,
    ai=None,
    keys=None,
    hat=None,
    player=None,
    tts=None,
    cls=RecordingListener,
    **overrides,
):
    """Build a RecordingListener with fast test timeouts.

    A truthy ``tts`` engine (a fresh :class:`FakeTts` by default) enables the
    spoken-echo path; ``RecordingListener._speak`` records the transcript
    instead of touching pyaudio.
    """
    settings = {
        "phrase": "hey nomon",
        "variants": ["hey no man"],
        "input_device_index": 1,
        "command_timeout_s": 0.4,
        "max_utterance_s": 2.0,
        "followup_window_s": 0.4,
        "ai_timeout_s": 5.0,
        "chime_volume_pct": 50,
        "rms_threshold": 0,
    }
    settings.update(overrides)
    return cls(
        stt_engine=engine,
        ai_service=ai if ai is not None else FakeAiService(),
        ai_key_store=keys if keys is not None else FakeKeyStore(),
        get_player=lambda: player,
        get_hat=lambda: hat,
        get_chime_dir=lambda: tmp_path / "chimes",
        tts_engine=tts if tts is not None else FakeTts(),
        **settings,
    )


# ============================================================================
# Pure helpers
# ============================================================================


def test_build_grammar_normalizes_dedupes_and_appends_unk():
    grammar = _build_grammar("  Hey  NOMON ", ["hey no man", "Hey Nomon", ""])
    assert grammar == '["hey nomon", "hey no man", "[unk]"]'


def test_parse_variants_splits_and_normalizes():
    assert parse_variants(" Hey No Man ,,hey gnome on , hey no man") == [
        "hey no man",
        "hey gnome on",
    ]
    assert parse_variants(["A B", "a  b", " "]) == ["a b"]


def test_resample_identity_at_16k():
    assert _resample_to_16k(LOUD_FRAME, 16000) == LOUD_FRAME


@pytest.mark.parametrize("rate", [48000, 44100])
def test_resample_downsamples_to_1600_samples_per_100ms(rate):
    n_src = rate // 10  # 100 ms
    pcm = struct.pack(f"<{n_src}h", *([1000] * n_src))
    out = _resample_to_16k(pcm, rate)
    assert len(out) % 2 == 0
    n_out = len(out) // 2
    assert n_out == 1600
    # Unity-DC-gain filter passes a constant through unchanged once past the
    # short start-up transient (history primed with zeros).
    samples = struct.unpack(f"<{n_out}h", out)
    assert all(abs(v - 1000) <= 1 for v in samples[100:1500])


@pytest.mark.parametrize("rate", [48000, 44100])
def test_resample_attenuates_above_nyquist(rate):
    # A tone above the 8 kHz target Nyquist would alias into the speech band
    # with naive decimation; the anti-alias FIR must suppress it. Compare its
    # residual against a passband tone that should survive.
    n = (rate // 10) * 4  # 400 ms — long enough to reach steady state
    passband = _resample_to_16k(_tone_pcm(1000, rate, n), rate)
    aliasing = _resample_to_16k(_tone_pcm(12000, rate, n), rate)

    def steady_rms(pcm: bytes) -> float:
        vals = _unpack_pcm(pcm)[200:-50]  # skip warm-up and tail
        return math.sqrt(sum(v * v for v in vals) / len(vals))

    rms_pass = steady_rms(passband)
    rms_alias = steady_rms(aliasing)
    assert rms_pass > 3000  # 1 kHz survives (input peak 10000)
    assert rms_alias < 0.15 * rms_pass  # 12 kHz strongly rejected


def test_resampler_streaming_matches_oneshot():
    # Chunked processing through a persistent resampler must equal processing
    # the whole signal at once — i.e. filter history joins frames seamlessly.
    rate = 48000
    frame_samples = rate // 10  # 100 ms
    frame_bytes = frame_samples * 2  # 16-bit mono
    whole_pcm = _tone_pcm(1500, rate, frame_samples * 3)
    one_shot = _Resampler(rate).process(whole_pcm)

    streaming = _Resampler(rate)
    chunks = b"".join(
        streaming.process(whole_pcm[i * frame_bytes : (i + 1) * frame_bytes]) for i in range(3)
    )
    assert chunks == one_shot


def test_resampler_reset_clears_history():
    rate = 48000
    r = _Resampler(rate)
    tone = _tone_pcm(1500, rate, rate // 10)
    first = r.process(tone)
    r.reset()
    after_reset = r.process(tone)
    # After reset the second frame reproduces the first (same zero-primed start).
    assert after_reset == first


def test_rms_silence_and_tone():
    assert _rms(SILENT_FRAME) == 0.0
    assert _rms(b"") == 0.0
    assert _rms(LOUD_FRAME) == pytest.approx(12000.0)


def test_trim_history_drops_oldest_pairs_keeping_alternation():
    messages = []
    for i in range(13):
        messages.append({"role": "user", "content": f"u{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})
    trimmed = _trim_history(messages)
    assert len(trimmed) == 20
    assert trimmed[0] == {"role": "user", "content": "u3"}
    assert [m["role"] for m in trimmed[:2]] == ["user", "assistant"]


def test_synthesize_chimes_writes_four_valid_wavs(tmp_path):
    paths = synthesize_chimes(tmp_path / "chimes")
    assert sorted(paths) == ["error", "processing", "success", "wake"]
    sizes = set()
    for path in paths.values():
        with wave.open(str(path), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 44100
            assert wf.getnframes() > 0
            sizes.add(wf.getnframes())
    assert len(sizes) == 4  # distinct chimes


def test_synthesize_chimes_preserves_existing_files(tmp_path):
    chime_dir = tmp_path / "chimes"
    chime_dir.mkdir()
    custom = chime_dir / "wake.wav"
    custom.write_bytes(b"operator-supplied")
    paths = synthesize_chimes(chime_dir)
    assert custom.read_bytes() == b"operator-supplied"
    assert paths["success"].exists() and paths["error"].exists()


def test_env_fallbacks(monkeypatch, tmp_path):
    monkeypatch.setenv("NOMON_WAKE_PHRASE", "  Hey  Nomon ")
    monkeypatch.setenv("NOMON_WAKE_PHRASE_VARIANTS", "hey no man, hey gnome on")
    monkeypatch.setenv("NOMON_WAKE_COMMAND_TIMEOUT_S", "junk")
    monkeypatch.setenv("NOMON_WAKE_FOLLOWUP_WINDOW_S", "999")  # out of range
    monkeypatch.setenv("NOMON_WAKE_RMS_THRESHOLD", "-5")  # out of range
    monkeypatch.delenv("NOMON_WAKE_INPUT_INDEX", raising=False)
    monkeypatch.setenv("NOMON_AUDIO_INPUT_INDEX", "3")  # must NOT leak into wake
    monkeypatch.setenv("NOMON_WAKE_INPUT_NAME", " PCM2902 ")
    monkeypatch.delenv("NOMON_WAKE_CHIME_VOLUME_PCT", raising=False)
    monkeypatch.setenv("NOMON_AUDIO_VOLUME", "70")
    listener = WakeWordListener(
        stt_engine=FakeEngine(FakeRecognizer(), FakeRecognizer()),
        ai_service=FakeAiService(),
        ai_key_store=FakeKeyStore(),
        get_player=lambda: None,
        get_hat=lambda: None,
        get_chime_dir=lambda: tmp_path,
    )
    assert listener.phrase == "hey nomon"
    assert listener.variants == ["hey no man", "hey gnome on"]
    assert listener._command_timeout_s == 8.0
    assert listener.followup_window_s == 8.0
    assert listener._rms_threshold == 300
    # ALSA-style numeric defaults are gone (they segfaulted libportaudio on
    # the Pi); unset index means auto-detect by name.
    assert listener._input_device_index is None
    assert listener._input_name_match == "pcm2902"
    assert listener._chime_volume_pct == 70


def test_env_explicit_input_index_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NOMON_WAKE_INPUT_INDEX", "5")
    listener = _make_listener(
        FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path, input_device_index=None
    )
    assert listener._input_device_index == 5


# ============================================================================
# Capture-device resolution (regression: blind index 2 segfaulted libportaudio)
# ============================================================================


def _pa_with_devices(devices, default_input_index=None, default_output_index=None):
    pa = MagicMock()
    pa.get_device_count.return_value = len(devices)
    pa.get_device_info_by_index.side_effect = lambda i: devices[i]
    if default_input_index is None:
        pa.get_default_input_device_info.side_effect = OSError("no default input device")
    else:
        pa.get_default_input_device_info.return_value = {
            "index": default_input_index,
            **devices[default_input_index],
        }
    if default_output_index is None:
        pa.get_default_output_device_info.side_effect = OSError("no default output device")
    else:
        pa.get_default_output_device_info.return_value = {
            "index": default_output_index,
            **devices[default_output_index],
        }
    return pa


def _auto_listener(tmp_path):
    return _make_listener(
        FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path, input_device_index=None
    )


def test_resolve_prefers_named_input_device(tmp_path):
    """The USB mic wins by name; ALSA's virtual 'default' (which advertises
    capture it cannot deliver and segfaults libportaudio) is never picked."""
    pa = _pa_with_devices(
        [
            {"name": "vc4-hdmi", "maxInputChannels": 0},
            {"name": "USB PnP Sound Device: Audio (hw:1,0)", "maxInputChannels": 1},
            {"name": "default", "maxInputChannels": 32},
        ]
    )
    assert _auto_listener(tmp_path)._resolve_input_device(pa) == 1


def test_resolve_falls_back_to_default_input(tmp_path):
    pa = _pa_with_devices(
        [{"name": "fancy array mic", "maxInputChannels": 2}], default_input_index=0
    )
    assert _auto_listener(tmp_path)._resolve_input_device(pa) == 0


def test_resolve_none_when_no_input_devices(tmp_path):
    pa = _pa_with_devices([{"name": "hdmi out", "maxInputChannels": 0}])
    assert _auto_listener(tmp_path)._resolve_input_device(pa) is None


def test_resolve_explicit_override_skips_enumeration(tmp_path):
    listener = _make_listener(
        FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path, input_device_index=7
    )
    pa = MagicMock()
    assert listener._resolve_input_device(pa) == 7
    pa.get_device_count.assert_not_called()


def test_resolve_output_device_prefers_named(tmp_path):
    """Chimes route to the USB codec's playback side, not the silent HDMI."""
    pa = _pa_with_devices(
        [
            {"name": "vc4-hdmi", "maxInputChannels": 0, "maxOutputChannels": 2},
            {"name": "USB PnP Sound Device", "maxInputChannels": 1, "maxOutputChannels": 2},
        ]
    )
    assert _auto_listener(tmp_path)._resolve_output_device(pa) == 1


def test_resolve_output_device_falls_back_to_default_output(tmp_path):
    pa = _pa_with_devices(
        [{"name": "hdmi", "maxInputChannels": 0, "maxOutputChannels": 2}],
        default_output_index=0,
    )
    assert _auto_listener(tmp_path)._resolve_output_device(pa) == 0


def test_resolve_output_device_none_when_no_output_devices(tmp_path):
    pa = _pa_with_devices([{"name": "usb mic only", "maxInputChannels": 1}])
    assert _auto_listener(tmp_path)._resolve_output_device(pa) is None


def test_resolve_output_device_explicit_override(tmp_path):
    listener = _make_listener(
        FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path, output_device_index=9
    )
    pa = MagicMock()
    assert listener._resolve_output_device(pa) == 9
    pa.get_device_count.assert_not_called()


def test_play_chime_routes_to_named_output(monkeypatch, tmp_path):
    pa = _patch_pyaudio(monkeypatch)
    infos = [
        {"name": "vc4-hdmi", "maxInputChannels": 0, "maxOutputChannels": 2},
        {"name": "USB PnP Sound Device", "maxInputChannels": 1, "maxOutputChannels": 2},
    ]
    pa.get_device_count.return_value = len(infos)
    pa.get_device_info_by_index.side_effect = lambda i: infos[i]
    listener = _bare_listener(tmp_path, hat=None, player=None)
    listener._chime_paths = synthesize_chimes(tmp_path / "chimes")

    listener._play_chime("wake")

    output_calls = [c for c in pa.open.call_args_list if c.kwargs.get("output")]
    assert output_calls and output_calls[0].kwargs["output_device_index"] == 1


def test_open_stream_auto_detects_usb_device(monkeypatch, tmp_path, bg_loop):
    """End-to-end: with no index configured the USB device gets opened."""
    stream = FakeStream([LOUD_FRAME])
    pa = _patch_pyaudio(monkeypatch, mic_stream=stream)
    infos = [
        {"name": "vc4-hdmi", "maxInputChannels": 0},
        {"name": "USB PnP Sound Device: Audio (hw:1,0)", "maxInputChannels": 1},
        {"name": "default", "maxInputChannels": 32},
    ]
    pa.get_device_count.return_value = len(infos)
    pa.get_device_info_by_index.side_effect = lambda i: infos[i]
    listener = _make_listener(
        FakeEngine(FakeRecognizer([("final", "hey nomon")]), FakeRecognizer()),
        tmp_path,
        input_device_index=None,
    )
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: pa.open.called)
        assert wait_for(lambda: len(listener.chimes) >= 1)  # wake heard via that device
    finally:
        listener.stop()
    assert pa.open.call_args_list[0].kwargs["input_device_index"] == 1


# ============================================================================
# start_background gating
# ============================================================================


def test_start_background_without_pyaudio_returns_false(monkeypatch, tmp_path, bg_loop):
    monkeypatch.setattr(wake, "_PYAUDIO_AVAILABLE", False)
    listener = _make_listener(FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path)
    assert listener.start_background(bg_loop) is False
    assert not listener.is_running and not listener.enabled


def test_start_background_without_phrase_returns_false(monkeypatch, tmp_path, bg_loop):
    _patch_pyaudio(monkeypatch)
    listener = _make_listener(FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path, phrase="")
    assert listener.start_background(bg_loop) is False
    assert not listener.is_running


def test_start_background_without_loop_returns_false(monkeypatch, tmp_path):
    _patch_pyaudio(monkeypatch)
    listener = _make_listener(FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path)
    assert listener.start_background() is False


def test_recognizer_unavailable_disables_listener(monkeypatch, tmp_path, bg_loop):
    _patch_pyaudio(monkeypatch)
    engine = FakeEngine(FakeRecognizer(), FakeRecognizer(), fail=SttUnavailableError("no model"))
    listener = _make_listener(engine, tmp_path)
    assert listener.start_background(bg_loop) is True
    assert wait_for(lambda: not listener.is_running)
    assert listener.enabled is False
    assert listener.state == "unavailable"


def test_mic_open_error_retries_with_backoff(monkeypatch, tmp_path, bg_loop):
    _patch_pyaudio(monkeypatch, open_error=OSError("no such device"))
    listener = _make_listener(FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path)
    try:
        assert listener.start_background(bg_loop) is True
        assert wait_for(lambda: listener.state == "mic_unavailable")
        assert listener.is_running  # still retrying, not dead
    finally:
        listener.stop()
    assert not listener.is_running


# ============================================================================
# State machine
# ============================================================================


def test_happy_path_wake_command_dispatch(monkeypatch, tmp_path, bg_loop):
    wake_rec = FakeRecognizer([("final", "hey nomon")])
    cmd_rec = FakeRecognizer([("final", "drive forward")])
    stream = FakeStream([LOUD_FRAME])
    _patch_pyaudio(monkeypatch, mic_stream=stream)
    ai = FakeAiService(reply="Rolling.")
    engine = FakeEngine(wake_rec, cmd_rec)
    listener = _make_listener(engine, tmp_path, ai=ai)
    try:
        assert listener.start_background(bg_loop) is True
        assert wait_for(
            lambda: listener.chimes == ["wake", "processing", "success"] and len(ai.calls) == 1
        )
    finally:
        listener.stop()
    assert ai.calls[0] == [{"role": "user", "content": "drive forward"}]
    assert listener.spoken == ["drive forward"]  # transcript echoed back
    # Wake recognizer got a grammar; command recognizer did not.
    grammars = [grammar for _rate, grammar in engine.calls]
    assert grammars[0] is not None and '"hey nomon"' in grammars[0]
    assert grammars[1] is None


def test_transcript_spoken_concurrently_with_dispatch(monkeypatch, tmp_path, bg_loop):
    """The spoken echo overlaps the AI dispatch, not runs after it."""
    speak_started = threading.Event()
    observed: dict[str, bool] = {}

    class SignalingListener(RecordingListener):
        def _speak(self, text: str) -> None:
            self.spoken.append(text)
            speak_started.set()

    class GatedAi(FakeAiService):
        async def run_command(self, messages, api_key):
            # Runs on the app loop while the listener thread is inside dispatch.
            # If the echo were sequential (spoken only after dispatch returns),
            # the event would never be set here and this wait returns False.
            loop = asyncio.get_running_loop()
            observed["overlap"] = await loop.run_in_executor(None, speak_started.wait, 2.0)
            return await super().run_command(messages, api_key)

    wake_rec = FakeRecognizer([("final", "hey nomon")])
    cmd_rec = FakeRecognizer([("final", "drive forward")])
    _patch_pyaudio(monkeypatch, mic_stream=FakeStream([LOUD_FRAME]))
    listener = _make_listener(
        FakeEngine(wake_rec, cmd_rec), tmp_path, ai=GatedAi(reply="ok"), cls=SignalingListener
    )
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: listener.chimes == ["wake", "processing", "success"])
    finally:
        listener.stop()
    assert listener.spoken == ["drive forward"]
    assert observed.get("overlap") is True


def test_unk_result_does_not_wake(monkeypatch, tmp_path, bg_loop):
    wake_rec = FakeRecognizer([("final", "[unk]"), ("final", "hey [unk]")])
    stream = FakeStream([LOUD_FRAME, LOUD_FRAME])
    _patch_pyaudio(monkeypatch, mic_stream=stream)
    ai = FakeAiService()
    listener = _make_listener(FakeEngine(wake_rec, FakeRecognizer()), tmp_path, ai=ai)
    try:
        assert listener.start_background(bg_loop) is True
        assert wait_for(lambda: len(wake_rec.accepted) >= 2)
        time.sleep(0.1)
    finally:
        listener.stop()
    assert listener.chimes == []
    assert ai.calls == []


def test_variant_match_wakes(monkeypatch, tmp_path, bg_loop):
    wake_rec = FakeRecognizer([("final", "[unk] hey no man")])
    stream = FakeStream([LOUD_FRAME])
    _patch_pyaudio(monkeypatch, mic_stream=stream)
    listener = _make_listener(FakeEngine(wake_rec, FakeRecognizer()), tmp_path)
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: len(listener.chimes) >= 1)
    finally:
        listener.stop()
    assert listener.chimes[0] == "wake"


def test_followup_carries_history_and_expiry_resets(monkeypatch, tmp_path, bg_loop):
    wake_rec = FakeRecognizer([("final", "hey nomon")])
    cmd_rec = FakeRecognizer([("final", "drive forward"), ("final", "turn left")])
    stream = FakeStream([LOUD_FRAME])
    _patch_pyaudio(monkeypatch, mic_stream=stream)
    ai = FakeAiService(reply="Okay.")
    listener = _make_listener(FakeEngine(wake_rec, cmd_rec), tmp_path, ai=ai)
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: len(ai.calls) == 2)
        # Second wake after the follow-up window expires starts fresh.
        assert wait_for(lambda: listener.state == "listening")
        wake_rec.script.append(("final", "hey nomon"))
        cmd_rec.script.append(("final", "stop now"))
        stream.frames.append(LOUD_FRAME)
        assert wait_for(lambda: len(ai.calls) == 3)
    finally:
        listener.stop()
    assert ai.calls[0] == [{"role": "user", "content": "drive forward"}]
    assert ai.calls[1] == [
        {"role": "user", "content": "drive forward"},
        {"role": "assistant", "content": "Okay."},
        {"role": "user", "content": "turn left"},
    ]
    assert ai.calls[2] == [{"role": "user", "content": "stop now"}]
    assert listener.chimes == [
        "wake",
        "processing",
        "success",
        "processing",
        "success",
        "wake",
        "processing",
        "success",
    ]
    # Every heard utterance (initial + follow-up + re-wake) is echoed in order.
    assert listener.spoken == ["drive forward", "turn left", "stop now"]


def test_no_api_key_plays_error_chime_without_dispatch(monkeypatch, tmp_path, bg_loop):
    wake_rec = FakeRecognizer([("final", "hey nomon")])
    cmd_rec = FakeRecognizer([("final", "drive forward")])
    stream = FakeStream([LOUD_FRAME])
    _patch_pyaudio(monkeypatch, mic_stream=stream)
    ai = FakeAiService()
    listener = _make_listener(
        FakeEngine(wake_rec, cmd_rec), tmp_path, ai=ai, keys=FakeKeyStore(key="")
    )
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: listener.chimes == ["wake", "processing", "error"])
    finally:
        listener.stop()
    assert ai.calls == []


def test_provider_error_plays_error_chime(monkeypatch, tmp_path, bg_loop):
    wake_rec = FakeRecognizer([("final", "hey nomon")])
    cmd_rec = FakeRecognizer([("final", "drive forward")])
    _patch_pyaudio(monkeypatch, mic_stream=FakeStream([LOUD_FRAME]))
    ai = FakeAiService(fail=AiProviderError("rate limited"))
    listener = _make_listener(FakeEngine(wake_rec, cmd_rec), tmp_path, ai=ai)
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: listener.chimes == ["wake", "processing", "error"])
    finally:
        listener.stop()
    assert len(ai.calls) == 1


def test_silence_after_wake_plays_error_chime(monkeypatch, tmp_path, bg_loop):
    wake_rec = FakeRecognizer([("final", "hey nomon")])
    cmd_rec = FakeRecognizer()  # never produces text
    _patch_pyaudio(monkeypatch, mic_stream=FakeStream([LOUD_FRAME]))
    ai = FakeAiService()
    listener = _make_listener(FakeEngine(wake_rec, cmd_rec), tmp_path, ai=ai, command_timeout_s=0.2)
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: listener.chimes == ["wake", "error"])
    finally:
        listener.stop()
    assert ai.calls == []


def test_empty_reply_ends_conversation_after_success(monkeypatch, tmp_path, bg_loop):
    wake_rec = FakeRecognizer([("final", "hey nomon")])
    cmd_rec = FakeRecognizer([("final", "drive forward"), ("final", "should not be consumed")])
    _patch_pyaudio(monkeypatch, mic_stream=FakeStream([LOUD_FRAME]))
    ai = FakeAiService(reply="")
    listener = _make_listener(FakeEngine(wake_rec, cmd_rec), tmp_path, ai=ai)
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: listener.chimes == ["wake", "processing", "success"])
        time.sleep(0.2)  # would be long enough to consume a follow-up
    finally:
        listener.stop()
    assert len(ai.calls) == 1
    assert cmd_rec.script  # the second utterance was never captured


def test_rms_gate_skips_decoding_during_silence(monkeypatch, tmp_path, bg_loop):
    wake_rec = FakeRecognizer()
    stream = FakeStream()  # silence only
    _patch_pyaudio(monkeypatch, mic_stream=stream)
    listener = _make_listener(FakeEngine(wake_rec, FakeRecognizer()), tmp_path, rms_threshold=500)
    try:
        listener.start_background(bg_loop)
        # The gate engages after the hold window and resets pending state once.
        assert wait_for(lambda: wake_rec.reset_count == 1)
        fed = len(wake_rec.accepted)
        assert fed <= wake._GATE_HOLD_FRAMES
        time.sleep(0.3)
        assert len(wake_rec.accepted) == fed  # gated: nothing new decoded
        # Loud audio reopens the gate and can wake.
        wake_rec.script.append(("final", "hey nomon"))
        stream.frames.append(LOUD_FRAME)
        assert wait_for(lambda: len(listener.chimes) >= 1)
    finally:
        listener.stop()
    assert listener.chimes[0] == "wake"


def test_pause_and_resume(monkeypatch, tmp_path, bg_loop):
    stream = FakeStream()
    pa = _patch_pyaudio(monkeypatch, mic_stream=stream)
    listener = _make_listener(FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path)
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: listener.state == "listening")
        opens_before = pa.open.call_count

        listener.pause()
        assert listener.is_paused
        assert wait_for(lambda: listener.state == "paused")
        assert stream.closed

        listener.resume()
        assert wait_for(lambda: pa.open.call_count > opens_before)
        assert wait_for(lambda: listener.state == "listening")
    finally:
        listener.stop()
    assert not listener.is_running


# ============================================================================
# Persistent PyAudio host (one construct/terminate per thread lifetime, not
# one per stream-open/chime/TTS clip)
# ============================================================================


def test_single_pyaudio_host_reused_across_pause_resume(monkeypatch, tmp_path, bg_loop):
    pa = _patch_pyaudio(monkeypatch, mic_stream=FakeStream())
    listener = _make_listener(FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path)
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: listener.state == "listening")
        assert pa.host_factory.call_count == 1

        for _ in range(2):
            listener.pause()
            assert wait_for(lambda: listener.state == "paused")
            listener.resume()
            assert wait_for(lambda: listener.state == "listening")
    finally:
        listener.stop()
    assert pa.host_factory.call_count == 1  # one host for the whole thread lifetime


def test_single_pyaudio_host_reused_for_chime_and_speech(monkeypatch, tmp_path, bg_loop):
    """A full wake+command+TTS-echo cycle must not construct more than one host."""
    wake_rec = FakeRecognizer([("final", "hey nomon")])
    cmd_rec = FakeRecognizer([("final", "drive forward")])
    pa = _patch_pyaudio(monkeypatch, mic_stream=FakeStream([LOUD_FRAME]))
    tts = FakeTts(wav=_tiny_wav())
    listener = _make_listener(
        FakeEngine(wake_rec, cmd_rec), tmp_path, tts=tts, cls=WakeWordListener
    )
    try:
        listener.start_background(bg_loop)
        # wake + processing + spoken echo + success = 4 output streams.
        assert wait_for(lambda: len(pa.output_streams) >= 4)
    finally:
        listener.stop()
    assert pa.host_factory.call_count == 1


def test_pyaudio_host_terminated_once_on_stop(monkeypatch, tmp_path, bg_loop):
    pa = _patch_pyaudio(monkeypatch, mic_stream=FakeStream())
    listener = _make_listener(FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path)
    listener.start_background(bg_loop)
    assert wait_for(lambda: listener.is_running)
    listener.pause()
    assert wait_for(lambda: listener.state == "paused")
    listener.resume()
    assert wait_for(lambda: listener.state == "listening")
    listener.stop()
    assert pa.terminate.call_count == 1


def test_failed_open_resets_host_for_next_retry(monkeypatch, tmp_path, bg_loop):
    """A replugged/vanished device must not stay invisible for the thread's life."""
    pa = _patch_pyaudio(monkeypatch, open_error=OSError("no such device"))
    listener = _make_listener(FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path)
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: listener.state == "mic_unavailable")
        assert wait_for(lambda: pa.terminate.call_count >= 1)
        assert wait_for(lambda: pa.host_factory.call_count >= 2)
    finally:
        listener.stop()


# ============================================================================
# STT model unload (memory)
# ============================================================================


def test_unload_model_while_stopped_delegates_to_engine(tmp_path):
    engine = FakeEngine(FakeRecognizer(), FakeRecognizer())
    listener = _make_listener(engine, tmp_path)
    assert listener.unload_model() is True
    assert engine.unloaded is True


def test_unload_model_while_running_is_refused(monkeypatch, tmp_path, bg_loop):
    engine = FakeEngine(FakeRecognizer(), FakeRecognizer())
    _patch_pyaudio(monkeypatch, mic_stream=FakeStream())
    listener = _make_listener(engine, tmp_path)
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: listener.is_running)
        assert listener.unload_model() is False
        assert engine.unloaded is False
    finally:
        listener.stop()


def test_unload_model_after_stop_succeeds(monkeypatch, tmp_path, bg_loop):
    engine = FakeEngine(FakeRecognizer(), FakeRecognizer())
    _patch_pyaudio(monkeypatch, mic_stream=FakeStream())
    listener = _make_listener(engine, tmp_path)
    listener.start_background(bg_loop)
    assert wait_for(lambda: listener.is_running)
    listener.stop()
    assert not listener.is_running
    assert listener.unload_model() is True
    assert engine.unloaded is True


def test_stop_is_idempotent(monkeypatch, tmp_path, bg_loop):
    _patch_pyaudio(monkeypatch, mic_stream=FakeStream())
    listener = _make_listener(FakeEngine(FakeRecognizer(), FakeRecognizer()), tmp_path)
    listener.start_background(bg_loop)
    assert wait_for(lambda: listener.is_running)
    listener.stop()
    listener.stop()
    assert not listener.is_running and not listener.enabled


def test_set_phrase_config_applies_on_next_start(monkeypatch, tmp_path, bg_loop):
    engine = FakeEngine(FakeRecognizer(), FakeRecognizer())
    _patch_pyaudio(monkeypatch, mic_stream=FakeStream())
    listener = _make_listener(engine, tmp_path)
    listener.set_phrase_config(phrase="  Hey  Robot ", variants=["hey row bot"])
    assert listener.phrase == "hey robot"
    assert listener.variants == ["hey row bot"]
    try:
        listener.start_background(bg_loop)
        assert wait_for(lambda: bool(engine.calls))
    finally:
        listener.stop()
    grammar = engine.calls[0][1]
    assert grammar is not None and '"hey robot"' in grammar and '"hey row bot"' in grammar


# ============================================================================
# Chime playback (the real path, with fake pyaudio + HAT)
# ============================================================================


def _bare_listener(tmp_path, hat, player, tts=None):
    return WakeWordListener(
        stt_engine=FakeEngine(FakeRecognizer(), FakeRecognizer()),
        ai_service=FakeAiService(),
        ai_key_store=FakeKeyStore(),
        get_player=lambda: player,
        get_hat=lambda: hat,
        get_chime_dir=lambda: tmp_path / "chimes",
        phrase="hey nomon",
        chime_volume_pct=55,
        tts_engine=tts,
    )


def _tiny_wav(rate=44100, n=160):
    """A minimal valid mono 16-bit WAV clip as bytes."""
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{n}h", *([0] * n)))
    return buf.getvalue()


def test_play_chime_enables_amp_plays_and_disables(monkeypatch, tmp_path):
    pa = _patch_pyaudio(monkeypatch)
    hat = MagicMock()
    player = MagicMock(is_playing=False)
    listener = _bare_listener(tmp_path, hat, player)
    listener._chime_paths = synthesize_chimes(tmp_path / "chimes")

    listener._play_chime("wake")

    hat.enable_speaker.assert_called_once()
    hat.set_volume.assert_called_once_with(55)
    assert pa.output_streams and pa.output_streams[0].write.called
    hat.disable_speaker.assert_called_once()


def test_play_chime_skipped_while_rest_playback_active(monkeypatch, tmp_path):
    pa = _patch_pyaudio(monkeypatch)
    hat = MagicMock()
    player = MagicMock(is_playing=True)
    listener = _bare_listener(tmp_path, hat, player)
    listener._chime_paths = synthesize_chimes(tmp_path / "chimes")

    listener._play_chime("success")

    assert not pa.output_streams
    hat.enable_speaker.assert_not_called()
    hat.disable_speaker.assert_not_called()


def test_play_chime_without_hat_still_plays(monkeypatch, tmp_path):
    pa = _patch_pyaudio(monkeypatch)
    listener = _bare_listener(tmp_path, hat=None, player=None)
    listener._chime_paths = synthesize_chimes(tmp_path / "chimes")

    listener._play_chime("error")

    assert pa.output_streams and pa.output_streams[0].write.called


# ============================================================================
# Spoken transcript echo (TTS)
# ============================================================================


def test_speak_synthesises_and_plays_through_output(monkeypatch, tmp_path):
    pa = _patch_pyaudio(monkeypatch)
    hat = MagicMock()
    tts = FakeTts(wav=_tiny_wav())
    listener = _bare_listener(tmp_path, hat=hat, player=None, tts=tts)

    listener._speak("drive forward")

    assert tts.calls == ["drive forward"]
    assert pa.output_streams and pa.output_streams[0].write.called
    hat.enable_speaker.assert_called_once()
    hat.disable_speaker.assert_called_once()


def test_speak_skipped_when_tts_unavailable(monkeypatch, tmp_path):
    pa = _patch_pyaudio(monkeypatch)
    hat = MagicMock()
    tts = FakeTts(fail=wake.TtsUnavailableError("no espeak"))
    listener = _bare_listener(tmp_path, hat=hat, player=None, tts=tts)

    listener._speak("drive forward")  # must not raise

    assert tts.calls == ["drive forward"]
    assert not pa.output_streams  # synthesis failed → nothing played
    hat.enable_speaker.assert_not_called()


def test_speak_survives_synthesis_error(monkeypatch, tmp_path):
    _patch_pyaudio(monkeypatch)
    tts = FakeTts(fail=RuntimeError("boom"))
    listener = _bare_listener(tmp_path, hat=None, player=None, tts=tts)
    listener._speak("drive forward")  # swallowed, no crash
    assert tts.calls == ["drive forward"]


def test_speak_empty_wav_plays_nothing(monkeypatch, tmp_path):
    pa = _patch_pyaudio(monkeypatch)
    tts = FakeTts(wav=b"")  # e.g. whitespace-only transcript
    listener = _bare_listener(tmp_path, hat=None, player=None, tts=tts)
    listener._speak("   ")
    assert not pa.output_streams


def test_speak_async_none_without_engine(monkeypatch, tmp_path):
    _patch_pyaudio(monkeypatch)
    listener = _bare_listener(tmp_path, hat=None, player=None, tts=None)
    assert listener._speak_async("hi") is None


def test_speak_async_starts_thread(monkeypatch, tmp_path):
    _patch_pyaudio(monkeypatch)
    tts = FakeTts(wav=_tiny_wav())
    listener = _bare_listener(tmp_path, hat=None, player=None, tts=tts)
    thread = listener._speak_async("hi")
    assert thread is not None
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert tts.calls == ["hi"]
