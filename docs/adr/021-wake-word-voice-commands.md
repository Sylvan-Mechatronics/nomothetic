# ADR-021: On-Robot Wake-Word Voice Commands

**Status:** Accepted
**Date:** 2026-07-10
**Deciders:** Perceptua

---

## Context

Phase 26/28 (ADR-020) gave the operator hands-on voice control: the app records
a clip, `POST /api/ai/transcribe` recognises it on-device, and the text drives
the Claude relay (`POST /api/ai/command`). The next step is **hands-free**
control: the robot itself listens on its USB microphone for a catch phrase
("hey nomon"), chimes to show it is listening, captures the spoken command, and
runs it through the same relay — no phone in hand.

The robot already has every piece except the wake detector: the USB mic
(PCM2902, used by `AudioRecorder`), the Vosk model (Phase 28), the AI relay
and its key store (Phase 26), WAV playback (`AudioPlayer`) and the amp-enable
IPC (`enable_speaker`/`disable_speaker`). Wake-word engines evaluated:

1. **Vosk keyword grammar.** Stream the mic into a `KaldiRecognizer`
   constrained to `[phrase, variants…, "[unk]"]`, reusing the already-deployed
   model. No new dependency, no new model, and the phrase is a plain-text
   config value.
2. **openWakeWord.** Better rejection, but adds an ONNX runtime to the 512 MB
   Pi and every phrase change requires training a model — not text-configurable.
3. **Porcupine (Picovoice).** Best accuracy/CPU, but commercial licensing and
   a custom keyword file per phrase.

## Decision

**A `WakeWordListener` in nomothetic (`nomothetic.wake`) streams the USB mic
into a grammar-constrained recognizer built from the shared Vosk model, and
dispatches heard commands to the same `AiCommandService` the app uses.**
Option 1 — the phrase stays a config string (`NOMON_WAKE_PHRASE`) and nothing
new is installed.

Key mechanics:

- **One model in RAM.** `VoskSttEngine.create_recognizer()` is the new seam:
  the listener's wake (grammar) and command (full-vocabulary) recognizers share
  the engine's lazily-loaded model, so wake support never loads a second copy.
- **State machine, one daemon thread.** Listen (RMS-gated grammar decode) →
  wake chime → capture one utterance (Vosk endpointing, no-speech timeout,
  hard cap) → dispatch on the app's event loop → success/error chime →
  **follow-up window** (capture again without re-waking, conversation history
  carried, capped at 20 turns) → silence resets to listening.
- **Chimes, not speech.** Feedback is three synthesized WAV tones
  (wake/success/error) under `media/audio/chimes/` — written on first start,
  operator-replaceable, played through a dedicated PyAudio output wrapped in
  `enable_speaker`/`set_volume`/`disable_speaker`. No TTS dependency.
- **Mic discipline.** The stream is closed while chimes play (no self-hearing)
  and while the AI executes (the robot may move); the `/api/audio/record`
  endpoints `pause()`/`resume()` the listener so recordings never fight over
  the ALSA device.
- **Config + runtime control.** `NOMON_WAKE_*` env vars are the persistent
  boot config; `GET/PUT /api/voice/wake` reports status and applies in-memory
  updates (enable/disable, phrase, variants) — the tuning loop for finding
  variants the model actually hears.
- **Graceful degradation.** Missing pyaudio, vosk, model, or phrase → the
  listener logs and stays off; a vanished mic retries with backoff. The API is
  never affected (conditional-imports ADR).

## Why nomothetic (extending ADR-020's boundary)

ADR-020 stated "the robot's own microphone is not involved" — this ADR
deliberately extends that: the mic now feeds the **operator command path**,
and nothing else. Wake detection is still not cognition (ADR-004): no robot
state is read, no autonomy decision is made, and autonomon remains uninvolved.
The listener lives in nomothetic because everything it composes — the Vosk
model, the AI service, the key store, audio playback — already lives in this
process, and sharing the model is only possible in-process on a 512 MB device.
nomopractic is unchanged (the amp IPC already existed).

## Trade-offs

- **Always-on decode CPU.** Grammar-constrained decoding is cheap and the RMS
  gate (`NOMON_WAKE_RMS_THRESHOLD`) skips silent frames entirely, but the cost
  is nonzero. Accepted; tune the gate per robot.
- **Eager model RAM.** Enabling the wake phrase loads the Vosk model at
  listener start rather than ADR-020's first-request lazy load. Accepted —
  a wake listener that cannot hear until the first app transcription would be
  useless. Measure RSS at deploy.
- **Out-of-vocabulary phrases.** Vosk silently drops grammar words missing
  from the model's vocabulary — "nomon" almost certainly is. Mitigated by
  `NOMON_WAKE_PHRASE_VARIANTS` (in-vocabulary spellings accepted as wakes) and
  a documented on-device tuning loop (docs/pi_setup.md §5.2).
- **Open mic, local only.** Audio never leaves the device and is never stored;
  only the *transcribed text* of post-wake utterances goes to Anthropic, and
  transcripts appear in the journal logs. False wakes cost one Claude call
  (the tool surface is destructive-free and movement is TTL-leased).

## Consequences

- New module `nomothetic.wake` (`WakeWordListener`, `synthesize_chimes`) and
  `nomothetic.wake_routes` (`GET/PUT /api/voice/wake`); listener constructed in
  `create_app()`, started/stopped in the lifespan.
- `VoskSttEngine.create_recognizer()` added to `nomothetic.stt`.
- `NOMON_WAKE_*` env vars (`.env.device.example`), `[wakeword]` in
  `config.toml` + `scripts/start.sh`.
- `nomothetic-api.service` gains `SupplementaryGroups=audio` and deploy.sh
  adds the service user to `audio` — /dev/snd access the existing audio
  endpoints already needed.
- No new Python dependencies: the feature requires the existing `[stt]` and
  `[audio]` extras plus the fetched model.
