# ADR-020: On-Device Speech-to-Text for Voice Commands

**Status:** Accepted
**Date:** 2026-07-09
**Deciders:** Perceptua

---

## Context

nomotactic's AI command bar (nomotactic Phase 3, ADR-002 there) turns operator
chat into robot actions through the device's Claude relay (Phase 26,
`POST /api/ai/command`). The remaining deferred piece is **voice input**:
tap-to-speak in the command bar.

The relay itself cannot transcribe speech — the Anthropic Messages API accepts
text, images, and PDFs, but **no audio**. Speech-to-text must therefore happen
before text reaches the relay. Options evaluated:

1. **Phone-native STT** (`expo-speech-recognition` wrapping the iOS/Android
   recognisers). Streaming partials and zero robot load, but it hard-wires the
   platform recognisers: no way to swap in a different model or transcription
   service later, it requires an Expo dev build (breaking the Expo Go dev
   loop), and quality/behaviour differ per platform.
2. **On-device (robot-side) STT.** The app records a clip and uploads it; the
   Pi transcribes locally behind a server-side abstraction. One engine serves
   every client (Expo Go, web, built apps) and the engine is swappable.
3. **Cloud STT via the device.** Same upload path, but the device relays audio
   to a hosted transcription API. Adds a second provider account and sends
   operator audio off-device.

## Decision

**Voice clips are transcribed on the device by nomothetic, behind a pluggable
`SttEngine` protocol (`nomothetic.stt`), exposed as `POST /api/ai/transcribe`
on the AI router.** The first engine is **Vosk** with the small English model
— fully offline. The app records with `expo-audio`, uploads the clip, and
feeds the returned text to the same `/api/ai/command` path the keyboard uses.

Key mechanics:

- **Pluggable engine.** The route reads `app.state.stt_engine`; swapping the
  model or wiring a cloud service is a `create_app()` change (or a future
  config switch), not a route or client change. This flexibility is the main
  reason option 1 was rejected.
- **Lazy, serialized Vosk.** The model loads on the first transcription
  request, never at API startup, and recognition holds the engine lock so
  concurrent uploads queue instead of multiplying peak memory.
- **ffmpeg normalisation.** Uploads are converted to 16 kHz mono PCM by an
  ffmpeg subprocess, so the endpoint accepts whatever container each platform
  records (m4a/AAC, webm/Opus, wav) without Python codec dependencies.
- **Graceful degradation.** Missing `vosk` (the `[stt]` extra), model
  directory, or ffmpeg maps to HTTP 503 with an actionable message; the rest
  of the API is unaffected. Uploads are capped (`NOMON_STT_MAX_BYTES`,
  default 2 MB ≈ 15 s of compressed audio) and rate limited separately from
  the AI command limiter (`stt_limiter`, 20/min/IP) so a voice command
  (transcribe + command) does not double-count against the chat budget.

## Why nomothetic and not autonomon

ADR-004 (autonomon) makes autonomon the brain: interpretation of sensor data
lives above nomothetic. Transcribing an *operator's* voice command is not
robot cognition — it is part of the operator command path, exactly like the
Phase 26 relay ("operator convenience, not autonomy"). No robot state is read,
no autonomy decision is made, and the robot's own microphone is not involved.
The audio never touches nomopractic.

## Trade-offs

- **Memory on the Pi Zero 2W.** The small Vosk model claims a large slice of
  the 512 MB alongside the camera and API. Mitigations: lazy load, serialized
  recognition, and the endpoint being optional (no model installed → 503,
  everything else unaffected). Accepted deliberately in exchange for engine
  flexibility; measure RSS after first use during deployment verification.
- **No streaming partials.** Record-then-transcribe means the operator sees
  the transcript only after the clip uploads — phone-native STT would stream
  words as spoken. Acceptable for short command phrases.
- **Latency.** First request pays the model load (seconds); subsequent
  requests pay ffmpeg + recognition (roughly real-time on the A53 cores for
  short clips).

## Consequences

- New module `nomothetic.stt` (`SttEngine`, `VoskSttEngine`), `[stt]` extra,
  `POST /api/ai/transcribe`, `stt_rate_limit`, `NOMON_STT_MODEL_PATH` /
  `NOMON_STT_MAX_BYTES` env vars.
- `scripts/fetch_stt_model.sh` (+ `make fetch-stt-model`) installs the model;
  ffmpeg becomes an optional apt prerequisite (docs/pi_setup.md §5.1).
- nomotactic gains `lib/voice.ts` (record/upload/speak) and a mic button in
  `CommandInput` — no native modules, so Expo Go and web keep working.
- The engine seam is the extension point for future STT models or services.
