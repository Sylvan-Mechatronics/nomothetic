# nomon — Development Roadmap

## Status Summary

| Phase | Name | Status |
|-------|------|--------|
| 1 | Camera Module | ✅ Complete |
| 1.5 | MJPEG Stream Server | ✅ Complete |
| 2 | HTTPS REST API | ✅ Complete |
| 2.5 | Auth & Rate Limiting | ⊘ Superseded by Phase 13 |
| 3 | MQTT Telemetry | ✅ Complete |
| 5 | HAT Module Driver (Rust) | ✅ Complete |
| 6 | Motor API Endpoints | ✅ Complete |
| 7 | Vehicle Convenience API | ✅ Complete |
| 8 | Audio & Peripheral Expansion | ✅ Complete |
| 9 | Audio Levels Control | ✅ Complete |
| 10 | Calibration API | ✅ Complete |
| 11 | Routine API | ✅ Complete |
| 13 | Central Mode & Authentication | ✅ Complete |
| 14 | ArcadeDB Persistence Layer | ✅ Complete |
| 15 | Deploy Hardening | ✅ Complete |
| 16 | Security Hardening | ✅ Complete |
| 17 | Device-Mode Authentication | ✅ Complete |
| 18 | BLE Pairing Coordination | ⊘ Superseded by Phase 20 |
| 18.1 | BLE Simplification Coordination | ⊘ Superseded by Phase 20 |
| 19 | Service Env-File Hardening | ✅ Complete |
| 20 | BLE → Wi-Fi Soft AP Migration | ✅ Complete |
| 21 | HTTP AP Pairing Service | ✅ Complete |
| 22 | Clean AP/WiFi Mode Separation with Self-Signed Certs | ✅ Complete |
| 23 | Device Fleet Registration & Identity | ✅ Complete |
| 24 | Autonomy Routine Launcher (autonomon plugin handoff) | ✅ Complete |
| 25 | Fleet Telemetry History + Profile Editing | ✅ Complete |
| 26 | AI Chat-Command Relay (device mode) | ✅ Complete |
| 27 | Autonomy Telemetry Persistence (MQTT device→central) | ✅ Complete |
| 28 | Voice Command Transcription (on-device STT) | ✅ Complete |

**Test totals (current): 663 passing** (23 camera + 14 streaming + 168 API + 36 telemetry + 94 HAT + 19 audio + 18 auth + 29 central + 32 device-auth + 17 db + 41 pairing + 12 rate-limit + 6 mode + 15 network-provision + 13 token-store + 25 user-store + 22 fleet-store + 7 wifi-ap + 72 routine-launcher [10 catalogue + 17 control + 16 logs + 29 manager]; `ap_mode` tests removed — see ADR-016 amendment)

---

## Completed Phases

### Phase 1 — Camera Module (`nomothetic.camera`)

**Deliverables:**
- `Camera` class wrapping `picamera2` for OV5647 sensor
- Still image capture: `capture_image(filename)`
- Video recording: `start_recording(filename)` / `stop_recording()`
- JPEG frame generator: `get_jpeg_frame_generator()`
- Encoder selection: H264 (default, 5 Mbps) or MJPEG
- Filename-only security validation with path traversal protection
- 20 passing tests

**Hardware specs confirmed:**
- Default video: 1280×720 @ 30 fps
- Max still: 2592×1944 @ 15.63 fps

---

### Phase 1.5 — MJPEG Stream Server (`nomothetic.streaming`)

**Deliverables:**
- `StreamServer` class using Flask
- HTML viewer at `GET /` with dark-themed responsive layout
- MJPEG stream at `GET /stream` (multipart/x-mixed-replace)
- Blocking (`start()`) and background thread (`start_background()`) modes
- Optional dependency: Flask in `[web]` group
- 14 passing tests

---

### Phase 2 — HTTPS REST API (`nomothetic.api`)

**Deliverables:**
- `APIServer` class using FastAPI + uvicorn
- HTTPS with auto-generated self-signed certificates in `.certs/`
- 5 camera control endpoints (see architecture.md)
- Pydantic request/response models with UTC timestamps
- CORS middleware for mobile clients
- OpenAPI docs at `/docs` and `/redoc`
- Optional dependency: FastAPI, uvicorn, cryptography, python-multipart, python-dotenv in `[api]` group
- 26 passing tests

**Test totals: 63 passing (20 camera + 14 streaming + 26 API + 3 integration)**

---

### Phase 3 — MQTT Telemetry (`nomothetic.telemetry`)

**Deliverables:**
- `TelemetryPublisher` class using `paho-mqtt` 2.x
- Background daemon thread (non-blocking, REST API unaffected)
- Structured JSON telemetry payload (device ID, camera status, nomothetic version, UTC timestamp)
- Configurable broker host/port/topic/interval via `config.toml` (`[mqtt]` section)
- Device ID auto-detection: env var → `/proc/cpuinfo` Pi serial → hostname
- Reconnect/retry with exponential back-off (1 s → 60 s cap)
- Optional dependency: `paho-mqtt` in `[telemetry]` group
- 23 passing tests

---

### Phase 5 — HAT Module Driver (Rust, Separate Repo)

**Hardware confirmed:** SunFounder Robot HAT V4 on I2C bus 1, address `0x14`.
See [docs/pi_hardware.md](pi_hardware.md) for discovery details.

**Language & repo:** Rust, in the `nomopractic` repository (see ADR-006).
Rust is chosen for deterministic latency in GPIO/I2C timing-critical
operations. The Python modules remain in this repo — they are I/O-bound
and gain nothing from a Rust conversion.

**IPC:** Unix domain socket at `/run/nomopractic/nomopractic.sock` with NDJSON framing.
Full schema: [docs/hat_ipc_schema.md](hat_ipc_schema.md).
Python client: `nomothetic.hat.HatClient` — see [docs/hat_python_client.md](hat_python_client.md).

**Milestone 5.1 — IPC Schema & Scaffold:**
- [x] `docs/hat_ipc_schema.md` — full IPC protocol spec
- [x] `docs/hat_python_client.md` — Python client design
- [x] `nomopractic` repository scaffolded; health IPC working on Pi

**Milestone 5.2 — Battery + Servo (P0 deliverables):**
- [x] `nomopractic`: I2C, ADC, battery voltage (`get_battery_voltage` IPC method)
- [x] `nomopractic`: PWM, servo angle + TTL watchdog
- [x] `nomothetic.hat.HatClient` with `get_battery_voltage`, `set_servo_angle`, `reset_mcu`, `health` (20 tests)
- [x] `nomothetic.api` endpoints: `GET /api/hat/battery`, `POST /api/hat/servo`, `POST /api/hat/reset`
- [x] Mock-socket tests in `tests/test_hat.py`

**Milestone 5.3 — MCU Reset + GPIO (P1):**
- [x] GPIO named pins (D4/D5/MCURST/SW/LED), `reset_mcu` IPC method
- [x] `POST /api/hat/reset` endpoint (Python + Rust both complete)
- [x] OTA binary deploy script (`nomopractic/scripts/deploy.sh`)

**Milestone 5.4 — CI & Release pipeline:**
- [x] GitHub Actions CI for `nomopractic`: fmt + clippy + tests + cross-compile aarch64
- [x] GitHub Releases on `v*` tags with SHA-256 artifact manifest
- [x] GitHub Actions CI for `nomothetic`: lint + type-check + tests

**Milestone 5.5 — Daemon State Endpoints:**
- [x] `nomopractic`: `get_servo_status` (active leases) and `get_mcu_status` (reset counter) IPC methods
- [x] `nomothetic.hat.HatClient.get_servo_status()` / `get_mcu_status()` with typed dataclasses
- [x] `GET /api/hat/servo/status` and `GET /api/hat/mcu/status` REST endpoints
- [x] Mock-socket tests in `tests/test_hat.py`; API tests in `tests/test_api.py`

**Milestone 5.6 — Launch scripts:**
- [x] `config.toml` — unified configuration template (`[stream]`, `[api]`, `[hat]`, `[audio]`, `[mqtt]`, `[telemetry]`, `[logging]`)
- [x] `scripts/start.sh stream|api|all` — background launch with PID tracking and log file
- [x] `scripts/stop.sh stream|api|all` — graceful shutdown via PID file
- [x] `scripts/deploy.sh` — SSH deploy with rollback support
- [x] `Makefile` targets: `start-stream`, `start-api`, `stop-stream`, `stop-api`, `stop`, `deploy`

**Design constraints:**
- Cross-compiled for `aarch64-unknown-linux-gnu` (CI uses `cross`)
- `nomothetic.api` HAT endpoints return `503 Service Unavailable` if daemon not running
- Python tests mock the IPC socket — testable on any developer machine without Pi hardware

---

### Phase 6 — Motor API Endpoints

**Goal**: Expose the DC motor control IPC methods (`set_motor_speed`,
`stop_all_motors`, `get_motor_status`) implemented in `nomopractic` as REST
API endpoints in `nomothetic.api`, with a matching `HatClient` façade and
full mock-socket/unit test coverage.

#### 6.1 — HatClient Motor Methods (`nomothetic.hat`)
- [x] `MotorLeaseEntry` dataclass: `channel`, `ttl_remaining_ms`, `conn_id`
- [x] `MotorStatusResult` dataclass: `active_leases: list[MotorLeaseEntry]`
- [x] `set_motor_speed(channel, speed_pct, ttl_ms)` — validates channel 0–3,
      speed_pct −100.0–100.0; sends `set_motor_speed` IPC call
- [x] `stop_all_motors()` — sends `stop_all_motors` IPC call; returns `stopped` count
- [x] `get_motor_status()` — sends `get_motor_status`; returns `MotorStatusResult`

#### 6.2 — REST Endpoints (`nomothetic.api`)
- [x] `POST /api/hat/motor` — set a motor channel's speed
      Request: `{channel: 0–3, speed_pct: −100.0–100.0, ttl_ms: 100–5000}`
      Response: `{channel, speed_pct, timestamp}`
- [x] `POST /api/hat/motor/stop` — immediately stop all motors
      Response: `{stopped: N, timestamp}`
- [x] `GET /api/hat/motor/status` — return active motor TTL lease table
      Response: `{active_leases: [...], timestamp}`
- [x] `503` on `HatConnectionError`; `500` on `HatError`; `422` on invalid params

#### 6.3 — Tests
- [x] `tests/test_hat.py`: `set_motor_speed`, `stop_all_motors`, `get_motor_status`
      (success, validation errors, hardware error)
- [x] `tests/test_api.py`: all three motor endpoints (success, 503 no client,
      503 connection error, 500 hardware error, 422 invalid params)

---

### Phase 7 — Vehicle Convenience API

**Goal**: High-level REST endpoints and matching `HatClient` methods that
replace raw channel-index calls with named, coordinated vehicle commands.
Channel-to-peripheral mappings are owned by the `nomopractic` daemon config;
nomothetic simply calls the named IPC methods.

#### 7.1 — HatClient Vehicle Methods
- [x] `GrayscaleResult` dataclass: `channels: list[int]`, `values: list[int]`
- [x] `drive(speed_pct, ttl_ms)` → IPC `drive` (all motors in sync)
- [x] `steer(angle_deg, ttl_ms)` → IPC `steer`
- [x] `pan_camera(angle_deg, ttl_ms)` → IPC `pan_camera`
- [x] `tilt_camera(angle_deg, ttl_ms)` → IPC `tilt_camera`
- [x] `read_grayscale()` → IPC `read_grayscale`, returns `GrayscaleResult`
- [x] `ValueError` raised on out-of-range inputs before IPC call

#### 7.2 — REST Vehicle Endpoints
- [x] `POST /api/drive` — `{ speed_pct, ttl_ms? }` → `{ speed_pct, motors }`
- [x] `POST /api/steer` — `{ angle_deg, ttl_ms? }` → `{ angle_deg }`
- [x] `POST /api/camera/pan` — `{ angle_deg, ttl_ms? }` → `{ angle_deg }`
- [x] `POST /api/camera/tilt` — `{ angle_deg, ttl_ms? }` → `{ angle_deg }`
- [x] `GET /api/sensor/grayscale` → `{ channels, values }`
- [x] All endpoints tagged `"Vehicle"` in OpenAPI docs
- [x] 503 on daemon unavailable, 500 on hardware error, 422 on invalid params

#### 7.3 — Tests
- [x] `tests/test_hat.py`: 23 new tests (drive, steer, pan_camera, tilt_camera,
      read_grayscale — success, validation, error cases)
- [x] `tests/test_api.py`: 20 new tests for all 5 vehicle endpoints
- [x] `uv run pytest tests/` — 208 passing
- [x] `uv run ruff check src/ tests/` — 0 errors
- [x] `uv run black --check src/ tests/` — clean
- [x] `uv run mypy src/ tests/` — 0 errors

---

### Phase 8 — Audio & Peripheral Expansion

**Goal**: Expose the ultrasonic distance sensor and speaker amplifier enable
(both new in nomopractic Phase 8) as REST API endpoints, add USB microphone
recording and HifiBerry DAC playback via a new `nomothetic.audio` module, and
wire stream start/stop into the REST API.

#### 8.1 — HatClient Peripheral Methods (`nomothetic.hat`)
- [x] `UltrasonicResult` dataclass: `distance_cm: float`
- [x] `read_ultrasonic()` → `UltrasonicResult` (IPC `read_ultrasonic`)
- [x] `enable_speaker()` → `None` (IPC `enable_speaker`, asserts BCM 20 high)
- [x] `disable_speaker()` → `None` (IPC `disable_speaker`, asserts BCM 20 low)

#### 8.2 — Audio Module (`nomothetic.audio`)
- [x] New module `src/nomothetic/audio.py`
- [x] `AudioRecorder`: records USB mic (PCM2902, ALSA card 1) to WAV
  - `start(filename=None) -> str`: starts background recording thread; returns output path
  - `stop() -> str | None`: signals thread, finalises WAV; returns path or None
  - Auto-generated timestamped filename when `filename` is absent
- [x] `AudioPlayer`: plays WAV via HifiBerry DAC (default output device)
  - `play(filename)`: resolves bare names against `audio_dir`; starts background thread
  - `stop()`: signals thread early
- [x] `AudioStatus` dataclass: `recording`, `recording_file`, `playing`, `playback_file`
- [x] `list_audio_files(audio_dir=None) -> list[str]`: sorted WAV basenames
- [x] Optional pyaudio dependency: `RuntimeError` raised when not installed
- [x] Constants: `DEFAULT_AUDIO_DIR` (`$NOMON_MEDIA_DIR/audio`, derived from `NOMON_MEDIA_DIR` env),
      `DEFAULT_INPUT_DEVICE_INDEX` (`NOMON_AUDIO_INPUT_INDEX` env, default 2)
- [x] Optional dependency group `[audio]` — `pyaudio>=0.2.14`

#### 8.3 — REST Endpoints (`nomothetic.api`)
- [x] `GET /api/sensor/ultrasonic` → `{ distance_cm, timestamp }` (tag: Sensor)
- [x] `POST /api/hat/speaker` → `{ enabled, timestamp }` (tag: HAT)
- [x] `POST /api/stream/start` → `{ url, host, port, timestamp }` (tag: Stream)
  - Starts `StreamServer` in background; returns existing URL if already running
- [x] `POST /api/stream/stop` → `{ success, timestamp }` (tag: Stream)
- [x] `GET /api/stream/status` → `{ running, url, timestamp }` (tag: Stream)
- [x] `POST /api/audio/record/start` → `{ recording, filename, timestamp }` (tag: Audio)
- [x] `POST /api/audio/record/stop` → `{ recording, filename, timestamp }` (tag: Audio)
- [x] `POST /api/audio/play` → enables speaker, starts playback → `{ playing, filename, timestamp }`
- [x] `POST /api/audio/play/stop` → stops playback, disables speaker → `{ success, timestamp }`
- [x] `GET /api/audio/files` → `{ files, timestamp }` (tag: Audio)
- [x] `GET /api/audio/status` → `{ recording, recording_file, playing, playback_file, timestamp }`
- [x] `lifespan` initialises `AudioRecorder` and `AudioPlayer` on startup; tears them down on shutdown

#### 8.4 — Tests
- [x] `tests/test_hat.py`: 8 new tests (ultrasonic success/error; speaker enable/disable success/error)
- [x] `tests/test_audio.py`: 16 new tests (list_audio_files, AudioRecorder, AudioPlayer)
- [x] `tests/test_api.py`: 36 new tests covering all 11 new endpoints
- [x] `uv run pytest tests/` — 262 passing
- [x] `uv run ruff check src/ tests/` — 0 errors
- [x] `uv run black --check src/ tests/` — clean
- [x] `uv run mypy src/ tests/` — 0 errors

**Architecture notes:**
- Camera stays in `nomothetic` (Python libcamera; no HAT GPIO needed)
- USB microphone recording stays in `nomothetic` (USB audio, not HAT GPIO)
- Speaker GPIO enable (BCM 20) is controlled by `nomopractic`; audio output
  (PyAudio → HifiBerry DAC) is handled directly by `nomothetic`
- `POST /api/audio/play` automatically enables the speaker via HAT client
  before starting playback; `POST /api/audio/play/stop` disables it after

---

## Upcoming

### Phase 2.5 — Authentication & Rate Limiting (⊘ Superseded)

**Superseded by Phase 13 — Central Mode & Authentication.**
The original Phase 2.5 planned JWT tokens and API keys as an optional
enhancement to the device-mode API. Phase 13 delivers a more comprehensive
solution with config-driven API modes, self-hosted JWT auth, user management,
and fleet data endpoints (see ADR-010, ADR-011).

---

### v0.3.0 Release Prep

**Goal**: Consolidate configuration strategy, remove legacy setup files, and
confirm all new work passes checks before tagging.

- [x] Config strategy formalised: `.env` = secrets only; `config.toml` = safe
      defaults (committed to repo; no copy step required)
- [x] `config.toml` ships with new `[audio]`, `[mqtt]`, `[telemetry]` sections
- [x] `scripts/start.sh` extended to parse and export audio/MQTT/telemetry config
- [x] `requirements.txt` and `requirements-dev.txt` removed (superseded by
      `pyproject.toml` + `uv.lock`)
- [x] `docs/releases/` removed (GitHub auto-generates release notes from tags)
- [x] Version bumped to `0.3.0` in `pyproject.toml`
- [x] `uv run pytest tests/` — 262 passing
- [x] `uv run ruff check`, `black --check`, `mypy` — clean

---

### Phase 9 — Audio Levels Control (P1)

**Goal**: Add REST API endpoints (and matching `HatClient` methods) to control
output volume (HifiBerry DAC) and input gain (USB microphone PCM2902). Requires
corresponding IPC methods in `nomopractic` (see nomopractic Phase 9).

**Output Volume:**
- [x] `HatClient.set_volume(volume_pct: int)` — sends `set_volume` IPC call (0–100)
- [x] `HatClient.get_volume() -> int` — sends `get_volume` IPC call
- [x] `POST /api/audio/volume` request `{ volume_pct: 0–100 }` → response `{ volume_pct, timestamp }`
- [x] `GET /api/audio/volume` → response `{ volume_pct, timestamp }`
- [x] Pydantic models: `VolumeRequest`, `VolumeResponse`
- [x] Auto-apply configured default volume on `POST /api/audio/play` (best-effort, env `NOMON_AUDIO_VOLUME`)

**Input Gain:**
- [x] `HatClient.set_mic_gain(gain_pct: int)` — sends `set_mic_gain` IPC call (0–100)
- [x] `HatClient.get_mic_gain() -> int` — sends `get_mic_gain` IPC call
- [x] `POST /api/audio/mic-gain` request `{ gain_pct: 0–100 }` → response `{ gain_pct, timestamp }`
- [x] `GET /api/audio/mic-gain` → response `{ gain_pct, timestamp }`
- [x] Pydantic models: `MicGainRequest`, `MicGainResponse`
- [x] Auto-apply configured default input gain on `POST /api/audio/record/start` (best-effort, env `NOMON_AUDIO_MIC_GAIN`)

**Testing & Integration:**
- [x] New tests in `test_hat.py` and `test_api.py` for volume and mic gain endpoints
- [x] `pytest tests/` — 292 passing (at time of phase completion)
- [x] `black --check .` + `ruff check .` — clean

---

## Upcoming

### Phase 10 — Calibration API (P1)

**Goal**: Expose the nomopractic Calibration & Configuration layer (Phase 10) as
`HatClient` methods and REST endpoints. Operators tune and calibrate the robot
via the HTTPS API before engaging autonomous routines.

**Dependency**: Requires nomopractic Phase 10 (Calibration & Configuration).

#### 10.1 — HatClient Calibration Methods (`nomothetic.hat`)
- [x] `MotorCalibrationEntry` dataclass: `channel: int`, `speed_scale: float`, `deadband_pct: float`, `reversed: bool`
- [x] `ServoCalibrationEntry` dataclass: `servo: str`, `trim_us: int`
- [x] `GrayscaleCalibrationEntry` dataclass: `adc_channel: int`, `white_raw: int`, `black_raw: int`
- [x] `CalibrationSnapshot` dataclass: `motors: list[MotorCalibrationEntry]`, `servos: dict[str, ServoCalibrationEntry]`, `grayscale: list[GrayscaleCalibrationEntry]`
- [x] `GrayscaleCaptureResult` dataclass: `channel: int`, `adc_channel: int`, `surface: str`, `raw_value: int`, `stored: bool`
- [x] `NormalizedGrayscaleResult` dataclass: `channels: list[int]`, `normalized: list[float]`
- [x] `SaveCalibrationResult` dataclass: `saved: bool`, `path: str`
- [x] `get_calibration()` → `CalibrationSnapshot`
- [x] `set_motor_calibration(channel, speed_scale=None, deadband_pct=None, reversed=None)` → `MotorCalibrationEntry`
  - Raises `HatError(code="INVALID_PARAMS")` on out-of-range channel
- [x] `set_servo_calibration(servo, trim_us)` → `ServoCalibrationEntry`
  - `servo` is one of `"steering"`, `"camera_pan"`, `"camera_tilt"`; `HatError(code="INVALID_PARAMS")` otherwise
- [x] `calibrate_grayscale(channel, surface)` → `GrayscaleCaptureResult`
  - `channel`: sensor position index (0 = left, 1 = center, 2 = right)
  - `surface`: `"white"` or `"black"`
- [x] `save_calibration()` → `SaveCalibrationResult`
- [x] `reset_calibration()` → `bool`
- [x] `read_grayscale_normalized()` → `NormalizedGrayscaleResult`
  - Sends `read_grayscale_normalized` IPC call
  - Returns per-channel normalised values (0.0 = white/reflective, 1.0 = black/non-reflective)

#### 10.2 — REST Endpoints (`nomothetic.api`)
- [x] `GET /api/calibration` → `{ motors, servos, grayscale, timestamp }` — full snapshot
- [x] `PUT /api/calibration/motor/{channel}` — set motor calibration
  Body: `{ speed_scale?: float, deadband_pct?: float, reversed?: bool }`
  Response: `{ channel, speed_scale, deadband_pct, reversed, timestamp }`
- [x] `PUT /api/calibration/servo/{servo_name}` — set servo trim
  Body: `{ trim_us: int }`
  Response: `{ servo, trim_us, timestamp }`
- [x] `POST /api/calibration/grayscale/{channel}/capture` — live ADC capture for one surface
  Body: `{ surface: "white" | "black" }`
  Response: `{ channel, adc_channel, surface, raw_value, stored, timestamp }`
- [x] `POST /api/calibration/save` — persist calibration to file on device
  Response: `{ saved: bool, path: str, timestamp }`
- [x] `POST /api/calibration/reset` — revert in-memory calibration to defaults
  Response: `{ reset: bool, timestamp }`
- [x] `GET /api/sensor/grayscale/normalized` — per-channel normalised grayscale (requires calibration)
  Response: `{ channels: list[int], normalized: list[float], timestamp }`
- [x] All calibration endpoints tagged `"Calibration"` in OpenAPI docs; normalised sensor endpoint tagged `"Sensor"`
- [x] `422` on invalid servo name or out-of-range values; `503` on daemon unavailable
- [x] Pydantic models: `MotorCalibrationRequest`, `ServoCalibrationRequest`, `GrayscaleCaptureRequest`,
  `NormalizedGrayscaleResponse`, `SaveCalibrationResponse`, and matching response models

#### 10.3 — Tests
- [x] `tests/test_hat.py`: all seven `HatClient` calibration methods — success and error cases
  (`get_calibration` defaults, `set_motor_calibration` partial update, `set_motor_calibration` invalid channel,
  `set_servo_calibration` valid, `set_servo_calibration` invalid servo name, `calibrate_grayscale` success,
  `calibrate_grayscale` constraint violation, `save_calibration` success, `reset_calibration`,
  `read_grayscale_normalized` success, connection error on each method — ~14 test cases)
- [x] `tests/test_api.py`: all seven REST endpoints (success, `422` validation, `503` no daemon) — ~21 test cases
- [x] `uv run pytest tests/` — 332 passing
- [x] `uv run ruff check src/ tests/` — 0 errors
- [x] `uv run black --check src/ tests/` — clean
- [x] `uv run mypy src/ tests/` — 0 errors

#### Phase 10 Exit Criteria
- [x] `GET /api/calibration` returns all current calibration values for motors, servos, and grayscale sensors
- [x] `PUT /api/calibration/motor/0` with `{ "speed_scale": 1.2, "reversed": true }` affects subsequent `POST /api/drive` commands on the robot
- [x] `PUT /api/calibration/servo/steering` with `{ "trim_us": -50 }` shifts the physical steering centre
- [x] `POST /api/calibration/grayscale/0/capture` with `{ "surface": "white" }` stores the live ADC reading as the white reference
- [x] `GET /api/sensor/grayscale/normalized` returns 0.0–1.0 per channel after surface calibration
- [x] `POST /api/calibration/save` persists calibration across daemon restarts; response includes `path`
- [x] All tests pass

---

### Phase 11 — Routine API (P1)

**Goal**: Expose the nomopractic Routine Engine (Phase 11) as `HatClient`
methods and REST API endpoints. Remote clients can start, stop, and monitor
self-contained on-robot routines via HTTPS.

**Architecture note**: Routine *execution* lives entirely in nomopractic (Rust).
nomothetic is a thin façade — it calls three IPC methods and maps results onto
Pydantic models and HTTP status codes, exactly as the motor and vehicle APIs do.
The `feature/routines` branch is the target for Phase 11.

#### 11.1 — HatClient Routine Methods (`nomothetic.hat`)
- [x] `RoutineStartResult` dataclass: `name: str`, `started_at_uptime_s: int`
- [x] `RoutineStatusResult` dataclass: `running: bool`, `name: str | None`, `elapsed_s: int | None`, `obstacles_avoided: int | None`, `cliffs_avoided: int | None`
- [x] `RoutineStopResult` dataclass: `name: str`, `ran_for_s: int`, `obstacles_avoided: int`, `cliffs_avoided: int`, `stop_reason: str`
- [x] `start_routine(name, speed_pct?, obstacle_threshold_cm?, cliff_threshold_normalized?, max_duration_s?)` → `RoutineStartResult`
  - Raises `HatError(code="ALREADY_RUNNING", ...)` when a routine is already active
  - Raises `HatError(code="INVALID_PARAMS", ...)` for unknown routine name
- [x] `stop_routine()` → `RoutineStopResult`
  - Raises `HatError(code="INVALID_PARAMS", ...)` if no routine is running
- [x] `get_routine_status()` → `RoutineStatusResult`

#### 11.2 — REST Endpoints (`nomothetic.api`)
- [x] `POST /api/routine/start` — start a named routine  
  Request: `{ name: str, speed_pct?: float, obstacle_threshold_cm?: float, cliff_threshold_normalized?: float, max_duration_s?: int }`  
  Response: `{ name, started_at_uptime_s, timestamp }`  
  Errors: `422` on invalid/unknown name; `409 Conflict` on `ALREADY_RUNNING`; `503` when daemon unavailable
- [x] `POST /api/routine/stop` — stop the active routine  
  Response: `{ name, ran_for_s, obstacles_avoided, cliffs_avoided, stop_reason, timestamp }`  
  Errors: `409 Conflict` if no routine is running; `503` when daemon unavailable
- [x] `GET /api/routine/status` — query active routine  
  Response: `{ running, name, elapsed_s, obstacles_avoided, cliffs_avoided, timestamp }`  
  Errors: `503` when daemon unavailable
- [x] Pydantic models: `RoutineStartRequest`, `RoutineStartResponse`, `RoutineStopResponse`, `RoutineStatusResponse`
- [x] All endpoints tagged `"Routine"` in OpenAPI docs
- [x] `409 Conflict` (not `422`) mapped from `HatError(code="ALREADY_RUNNING")`

#### 11.3 — Tests
- [x] `tests/test_hat.py`: `start_routine` success, `start_routine` ALREADY_RUNNING, `start_routine` unknown name, `stop_routine` success, `stop_routine` not-running, `get_routine_status` idle, `get_routine_status` running, connection error
- [x] `tests/test_api.py`: `POST /api/routine/start` success, 409 ALREADY_RUNNING, 422 unknown name, 503 no daemon; `POST /api/routine/stop` success, 409 not running, 503; `GET /api/routine/status` idle, running, 503
- [x] `uv run pytest tests/` — 352 passing
- [x] `uv run ruff check src/ tests/` — 0 errors
- [x] `uv run black --check src/ tests/` — clean
- [x] `uv run mypy src/ tests/` — 0 errors

#### Phase 11 Exit Criteria
- [x] `POST /api/routine/start` with `{ "name": "explore" }` causes the robot to drive autonomously
- [x] `POST /api/routine/stop` halts all motors and returns telemetry stats
- [x] `GET /api/routine/status` shows live progress (elapsed time, avoidance counts)
- [x] Routine continues after REST client disconnects; stops only on explicit stop or max_duration timeout
- [x] All tests pass

---

### Phase 13 — Central Mode & Authentication (P1)

**Goal:** Run the same nomothetic codebase as a centrally-hosted API server
(in addition to the existing device-mode deployment on each Pi). Central mode
provides JWT authentication, user management, and fleet data endpoints.
Device mode is unchanged.

**Cross-repo dependencies:**
- nomographic V2 central migration (User + OwnsDevice schema)
- nomotactic Phase 1 (consumes auth and fleet endpoints)

**Architecture decisions:**
- ADR-010: Self-hosted JWT authentication
- ADR-011: Central vs device API mode

#### 13.1 — Config-Driven API Mode (`nomothetic.mode`)
- [x] New module `src/nomothetic/mode.py`: `Mode` enum (`device`, `central`),
      `get_mode()` reads `NOMON_API_MODE` env var (default `device`)
- [x] `create_app()` in `api.py` conditionally registers route groups:
  - Device mode: camera, HAT, vehicle, sensor, stream, audio, calibration,
    routine endpoints (existing behaviour)
  - Central mode: auth, fleet endpoints (new)
  - Both modes: health endpoint
- [x] `config.toml`: add `api_mode = "device"` to `[api]` section
- [x] Test fixture: `@pytest.fixture(params=["device", "central"])` for
      mode-switching tests
- [x] Verify: existing device-mode tests pass without changes
- [x] Mode-specific CORS: device mode allows all origins; central mode
      uses explicit `NOMON_CORS_ORIGINS` list

**Exit criteria:**
- ✅ Device mode: all existing endpoints work, no regressions
- ✅ Central mode: health endpoint returns ok; hardware endpoints not registered
- ✅ `pytest && ruff check . && black --check .`

#### 13.2 — JWT Auth Module (`nomothetic.auth`)
- [x] New module `src/nomothetic/auth.py`:
  - `AuthService` class: JWT creation, validation, password hashing
  - Access tokens: HS256, 15-min TTL, claims `{sub, exp, iat, iss}`
  - Refresh tokens: random bytes, 7-day TTL, stored hashed in ArcadeDB
  - Password hashing: bcrypt (10 rounds minimum)
  - `jwt_required` FastAPI dependency for route protection
  - JWT secret from `NOMON_JWT_SECRET` env var (validated at startup)
- [x] New optional dependency group `[auth]`: `authlib>=1.0`, `bcrypt>=4.0`
- [x] Conditional import: auth module only loaded in central mode
- [x] Auth rate limiting via `src/nomothetic/rate_limit.py`:
  - Sliding-window rate limiter (5/min login, 10/min register)
  - Per-IP tracking scoped to app state (isolated across test instances)
  - `NOMON_TRUST_PROXY` env var controls X-Forwarded-For trust
- [x] Tests: token creation/validation, password hashing, expired token
      rejection, refresh token rotation, rate limiting

**Exit criteria:**
- ✅ `AuthService` creates and validates JWT tokens
- ✅ bcrypt password hashing works
- ✅ `jwt_required` dependency returns 401 for missing/invalid tokens
- ✅ JWT secret not logged (security checklist P5)

#### 13.3 — User Management Endpoints (Central Mode)
- [x] `POST /api/auth/register` — create user account
  Request: `{ email, password, display_name }`
  Response: `{ user_id, email, display_name, timestamp }`
  Errors: `409` email already exists, `422` validation failure
- [x] `POST /api/auth/login` — authenticate and issue tokens
  Request: `{ email, password }`
  Response: `{ access_token, refresh_token, token_type: "bearer", expires_in }`
  Errors: `401` invalid credentials
- [x] `POST /api/auth/refresh` — rotate refresh token
  Request: `{ refresh_token }`
  Response: `{ access_token, refresh_token, token_type: "bearer", expires_in }`
  Errors: `401` invalid/expired refresh token
- [x] `GET /api/auth/me` — current user profile (requires JWT)
  Response: `{ email, display_name, created_at, last_login_at }`
- [x] All endpoints tagged `"Auth"` in OpenAPI docs
- [x] Pydantic models: `RegisterRequest`, `LoginRequest`, `RefreshRequest`,
      `TokenResponse`, `UserResponse`
- [x] CORS on auth routes: explicit origins from `NOMON_CORS_ORIGINS` env var
- [x] Tests: register, login, refresh, profile — success, validation, error cases

**Exit criteria:**
- ✅ Full auth flow works: register → login → access protected endpoint → refresh
- ✅ Duplicate email registration returns 409
- ✅ Invalid credentials return 401
- ✅ Refresh token rotation invalidates previous token

#### 13.4 — Fleet Data Endpoints (Central Mode)
- [x] New module `src/nomothetic/fleet_routes.py`: in-memory fleet data store
  (ArcadeDB integration deferred; transient store sufficient for MVP)
- [x] `POST /api/fleet/devices` — register a device and link to user
  Request: `{ vin, model }` (requires JWT) (`registration_proof` added in Phase 23 — see Phase 23 for details)
  Response: `{ vin, model, registered_at, timestamp }`
  Creates Vehicle vertex + OwnsDevice edge (role: `owner`)
- [x] `GET /api/fleet/devices` — list current user's devices
  Response: `{ devices: [{ vin, model, firmware_version, last_seen_at }], timestamp }`
- [x] `GET /api/fleet/devices/{vin}` — device detail with latest telemetry
  Response: `{ vin, model, firmware_version, last_seen_at, latest_telemetry: {...}, timestamp }`
- [x] `DELETE /api/fleet/devices/{vin}` — remove device ownership
  Response: `{ vin, removed, timestamp }`
  Removes OwnsDevice edge; does not delete Vehicle vertex
- [x] All endpoints tagged `"Fleet"`, require JWT
- [x] Tests: CRUD operations, authorization (user can only see own devices),
      error cases (device not found, unauthorized)

**Exit criteria:**
- ✅ User can register, associate, list, query, and disassociate devices
- ✅ Device queries scoped to authenticated user (no cross-user access)
- ✅ `pytest && ruff check . && black --check .`

#### Phase 13 Exit Criteria (aggregate)
- [x] Central mode serves auth + fleet endpoints; device mode unchanged
- [x] Full auth flow: register → login → manage devices → refresh token
- [x] No regressions in device-mode test suite (≥ 352 passing)
- [x] Central-mode test suite covers all new endpoints
- [x] `uv run pytest tests/` — 412 passing
- [x] `uv run ruff check . && uv run black --check .` — clean

---

### Phase 14 — ArcadeDB Persistence Layer (P1)

**Goal:** Replace the transient in-memory stores from Phase 13 with a
Protocol-based persistence abstraction supporting both in-memory (dev/test)
and ArcadeDB (production) backends via the HTTP Gremlin API.

**Architecture decisions:**
- ADR-012: ArcadeDB HTTP Gremlin API for persistence

**Cross-repo dependencies:**
- nomographic central migrations (V1 Vehicle schema, V2 User schema)

#### 14.1 — Database Client (`nomothetic.db`)
- [x] `DatabaseConfig` dataclass: `from_env()` reads `ARCADEDB_HOST`,
      `ARCADEDB_HTTP_PORT`, `ARCADEDB_DATABASE`,
      `ARCADEDB_ROOT_PASSWORD` from environment (user is always `root`)
- [x] `DatabaseClient` class: `httpx.AsyncClient` with Basic Auth,
      `execute_gremlin()`, `execute_sql()`, `health()`, `close()`
- [x] `DatabaseError` exception: `status_code`, `message`
- [x] Conditional import: `httpx` availability flag
- [x] 13 passing tests (`tests/test_db.py`)

#### 14.2 — User Store (`nomothetic.user_store`)
- [x] `UserStore` Protocol: `get_user`, `create_user`, `update_user`,
      `user_exists`
- [x] `InMemoryUserStore`: dict-backed implementation (extracted from
      AuthService)
- [x] `GremlinUserStore`: ArcadeDB-backed implementation with Gremlin
      traversals and `_sanitize_gremlin_value()` input validation
- [x] 19 passing tests (`tests/test_user_store.py`)

#### 14.3 — Fleet Store (`nomothetic.fleet_store`)
- [x] `FleetStore` Protocol: `get_devices`, `get_device`, `register_device`,
      `remove_device`, `device_exists`
- [x] `DeviceItem` model: moved from `fleet_routes.py`
- [x] `InMemoryFleetStore`: dict-backed implementation (extracted from
      `fleet_routes._FleetStore`)
- [x] `GremlinFleetStore`: ArcadeDB-backed implementation with Vehicle
      vertex and OwnsDevice edge traversals
- [x] 22 passing tests (`tests/test_fleet_store.py`)

#### 14.4 — AuthService Async Refactor
- [x] `AuthService.__init__` accepts optional `user_store` parameter
      (defaults to `InMemoryUserStore`)
- [x] `create_user`, `authenticate`, `get_user`, `refresh_token` are now
      `async def` — all call sites updated with `await`
- [x] `auth_routes.py` updated with `await` on all store-backed calls
- [x] `test_auth.py` tests converted to `@pytest.mark.asyncio` where needed
- [x] `pytest-asyncio>=0.21` added to dev dependencies

#### 14.5 — Fleet Routes Refactor
- [x] `_FleetStore` class removed from `fleet_routes.py`
- [x] Routes import `FleetStore`, `DeviceItem` from `fleet_store`
- [x] All store method calls use `await`
- [x] `set_fleet_store` / `get_fleet_store` accept/return `FleetStore`

#### 14.6 — Application Wiring (`nomothetic.api`)
- [x] `create_app()` checks `ARCADEDB_HOST` environment variable:
  - If set: creates `DatabaseClient`, `GremlinUserStore`, `GremlinFleetStore`
  - If not set: creates `InMemoryUserStore`, `InMemoryFleetStore`
- [x] `db_client` stored on `app.state` for lifespan cleanup
- [x] Lifespan shutdown closes `db_client` if present

#### Phase 14 Exit Criteria
- [x] All 412 pre-existing tests still pass (no regressions)
- [x] 54 new tests for db, user_store, and fleet_store modules
- [x] `uv run pytest tests/` — 466 passing
- [x] `uv run ruff check . && uv run black --check .` — clean
- [x] REST API contract unchanged — no breaking changes
- [x] In-memory stores used by default (tests and dev)
- [x] ArcadeDB stores activated when `ARCADEDB_HOST` is set

---

### Phase 15 — Deploy Hardening

**Goal:** Production-ready deployment infrastructure — systemd services,
environment templates, and deploy script integration.

**Architecture decisions:**
- ADR-013: Systemd service architecture

#### 15.1 — Systemd Service Files
- [x] `systemd/nomothetic-api.service` — device-mode API (uvicorn, port 8443)
  - After=network.target nomopractic.service; Wants=nomopractic.service
  - EnvironmentFile=-/etc/nomothetic/nomothetic.env (optional)
  - Environment=NOMON_API_MODE=device
- [x] `systemd/nomothetic-stream.service` — device-mode MJPEG stream (Flask, port 8000)
  - Standalone; no dependency on nomopractic
- [x] `systemd/nomothetic-central.service` — central-mode API (uvicorn, port 443, TLS)
  - EnvironmentFile=/etc/nomothetic/nomothetic.env (required)
  - Environment=NOMON_API_MODE=central
  - TLS certs at /etc/nomothetic/tls/
- [x] All services: User=nomon, Group=nomon, Restart=on-failure, journal logging

#### 15.2 — Environment Templates
- [x] `.env.example` updated with all known env vars:
  API mode, JWT, ArcadeDB, CORS, proxy trust, media, audio, MQTT, deploy
- [x] `nomotactic/.env.example` created with EXPO_PUBLIC_*_API_URL vars
- [x] `nomotactic/.gitignore` updated to ignore `.env`

#### 15.3 — Deploy Script Integration
- [x] `scripts/deploy.sh` extended with post-deploy systemd steps:
  - Copies service files to /etc/systemd/system/ if changed
  - Runs systemctl daemon-reload
  - Enables and restarts device-mode services
  - Gracefully skipped if systemd is not available

#### 15.4 — nomographic Local Deploy
- [x] `nomographic/scripts/deploy-local.sh` — deploy ArcadeDB local migrations to Pi
  - Accepts optional pi-host argument for remote execution via SSH
  - Rsyncs nomographic directory, ensures data directory, runs migrations

---

### Phase 16 — Security Hardening

**Goal:** Harden input validation, add token persistence layer, server-side
logout, device-mode TLS, and ArcadeDB TLS support.

#### 16.1 — Sanitizer & Input Validation
- [x] `_sanitize_gremlin_value()` in `user_store.py` and `fleet_store.py`
      now rejects null bytes and control characters (ord < 0x20)
- [x] `_ALLOWED_USER_UPDATE_FIELDS` whitelist restricts `update_user()` to
      `display_name`, `last_login_at`, `active` — prevents property injection
- [x] Tests: sanitizer and whitelist coverage in `test_user_store.py` and
      `test_fleet_store.py`

#### 16.2 — Token Store (`nomothetic.token_store`)
- [x] `TokenStore` Protocol: `store_token`, `get_email`, `delete_token`,
      `delete_tokens_for_user`, `cleanup_expired`
- [x] `InMemoryTokenStore`: dict-backed implementation with lazy expiry cleanup
- [x] `GremlinTokenStore`: ArcadeDB-backed implementation with Gremlin
      traversals and `_sanitize_gremlin_value()` input validation
- [x] `nomographic/central/sql/V3__create_refresh_token.sql` migration
- [x] 13 passing tests (`tests/test_token_store.py`)

#### 16.3 — AuthService Token Store Integration
- [x] `AuthService.__init__` accepts optional `token_store` parameter
      (defaults to `InMemoryTokenStore`)
- [x] `create_refresh_token` and `create_tokens` are now `async def`
- [x] `refresh_token` uses `TokenStore` instead of in-memory dict
- [x] `revoke_refresh_token` method added for server-side logout
- [x] `create_app()` wires `GremlinTokenStore` when ArcadeDB configured
- [x] All call sites updated with `await`

#### 16.4 — Logout Endpoint
- [x] `POST /api/auth/logout` — revoke refresh token (requires JWT)
  Request: `{ refresh_token }`, Response: `{ success, timestamp }`
  Idempotent — always returns 200 to avoid leaking token validity
- [x] nomotactic `logout()` calls server-side revocation (best-effort)
- [x] Tests: logout success, requires auth, invalid token still 200

#### 16.5 — Device-Mode TLS & CORS
- [x] `systemd/nomothetic-api.service` updated with `--ssl-keyfile` and
      `--ssl-certfile` flags for device-mode TLS
- [x] Device-mode CORS replaced `allow_origins=["*"]` with configurable
      `NOMON_CORS_ORIGINS` (default `https://10.0.0.1:8443`)

#### 16.6 — ArcadeDB TLS Support
- [x] `DatabaseConfig.use_tls` field reads `ARCADEDB_USE_TLS` env var
- [x] `DatabaseClient` uses `https://` when `use_tls=True`
- [x] Tests for TLS config and client URL scheme

#### 16.7 — Security Documentation
- [x] ADR-010 updated with Known Limitations (web token storage)
- [x] ADR-011 updated with Device-Mode Security Boundary section
- [x] Startup warning logged when Tailscale not detected in device mode
- [x] `nomographic/docker-compose.yml` requires `ARCADEDB_ROOT_PASSWORD`
- [x] `nomographic/.env.example` passwords changed to `changeme_before_deploy`

#### 16.8 — Network Provisioning Security (2026-06-01)
- [x] `api.py`: `WifiProvisionRequest._validate_ssid` rejects SSID values containing
      control characters (U+0000–U+001F, U+007F) and leading dashes via deny-list regex
      (`re.search(r'[\x00-\x1f\x7f]', v) or v.startswith('-')`) — prevents nmcli injection
- [x] `api.py`: pairing secret display file `/run/nomothetic/pairing-secret` opened with
      `0o600` (was `0o644`) — prevents world-readable secret exposure on the Pi filesystem
- [x] `fleet_routes.py`: `_validate_registration_proof()` expanded with FL1 docstring
      note documenting that the structural-only JWT validation is intentional (see ADR-017
      and security checklist FL1)
- [x] 6 new tests in `tests/test_network_provision.py` — SSID control-char rejection
      (`\x00`, `\x1f`, `\x7f`), leading-dash rejection, existing valid cases unaffected
- [x] `nomourgoi/docs/security-checklist.md` — added P22/P23 (SSID control-char and
      leading-dash validation), P24/P25 (pairing secret file permissions), X6–X8 (web
      token hygiene), FL1–FL3 (fleet registration proof structural validation)

---

### Phase 17 — Device-Mode Authentication

**Goal:** Add opt-in JWT authentication to device-mode endpoints so that
the on-robot API is protected without requiring a central server. A
one-time pairing flow (shared secret displayed at startup) issues
device-scoped JWTs with a separate issuer (`nomon-device`) to prevent
token reuse across modes.

**Architecture decisions:**
- ADR-014: Device-mode authentication

**Cross-repo dependencies:**
- nomotactic (pairing UI, device token management)
- nomourgoi (security checklist P18–P21)

#### 17.1 — Pairing Module (`nomothetic.pairing`)
- [x] `PairingState` class: `generate_secret()` (128-bit, `secrets.token_urlsafe`),
      `verify_and_consume()` (constant-time `hmac.compare_digest`, single-use),
      `is_paired()`, `reset()`
- [x] Auto-generated `jwt_secret` per pairing lifecycle (`secrets.token_urlsafe(48)`)
- [x] 14 passing tests (`tests/test_pairing.py`)

#### 17.2 — Device Auth Routes (`nomothetic.device_auth_routes`)
- [x] `GET /api/device/auth/status` — pairing state (unauthenticated)
- [x] `POST /api/device/auth/pair` — consume pairing secret, create
      `device-owner@local` user, issue tokens (rate-limited: 3/min)
- [x] `POST /api/device/auth/refresh` — rotate device refresh token
- [x] `GET /api/device/auth/me` — device owner profile (requires JWT)
- [x] 17 passing tests (`tests/test_device_auth.py`)

#### 17.3 — API Wiring
- [x] All device-mode endpoints wrapped in `APIRouter(dependencies=[Depends(jwt_required)])`
- [x] `AuthService` issuer parameterised: `nomon-device` (device) vs `nomon-central` (central)
- [x] Health endpoint remains unauthenticated
- [x] Opt-out via `NOMON_DEVICE_AUTH=false` env var (warning logged)
- [x] Pairing secret logged at startup for operator visibility
- [x] `pairing_rate_limit` added to `rate_limit.py`

#### 17.4 — nomotactic Integration
- [x] `lib/auth.tsx`: device token storage (`expo-secure-store`), `pairWithDevice()`,
      `unpairDevice()`, `refreshDeviceToken()`
- [x] `lib/api.ts`: per-base-URL token injection, device-aware 401 refresh
- [x] `app/index.tsx`: inline pairing prompt (secret + display name inputs)
- [x] TypeScript strict + ESLint clean

#### Phase 17 Exit Criteria
- [x] Device-mode endpoints require JWT when `NOMON_DEVICE_AUTH=true`
- [x] Pairing flow: secret displayed → operator enters in app → tokens issued
- [x] Central tokens rejected on device API (issuer isolation)
- [x] No regressions: 466 pre-existing tests pass with `NOMON_DEVICE_AUTH=false`
- [x] 31 new tests (14 pairing + 17 device auth)
- [x] `uv run pytest tests/` — 497 passing
- [x] `uv run ruff check . && uv run black --check .` — clean

---

### Phase 18 — BLE Pairing Coordination ⊘ Superseded by Phase 20

**Goal:** Coordinate BLE pairing between nomopractic (BLE GATT server) and
nomotactic (BLE client) by managing the shared pairing secret lifecycle and
providing WiFi provisioning support. nomothetic is NOT in the BLE data path —
BLE commands go directly from nomotactic to nomopractic. nomothetic's role is:

1. Generate and persist the pairing secret (shared with nomopractic)
2. Accept WiFi provisioning requests (when Pi joins WiFi via BLE-provided creds)
3. Document BLE prerequisites and setup

**Architecture decisions:**
- nomopractic ADR-001: BLE GATT server in nomopractic
- nomopractic ADR-003: BLE security model

**Cross-repo dependencies:**
- nomopractic Phase 13: BLE GATT server reads shared pairing secret
- nomotactic Phase 2: BLE client implementation

#### 18.1 — Shared Pairing Secret Management
- [x] `nomothetic/pairing.py`: update `PairingState.generate_secret()` to
      also write the pairing secret to a shared file at
      `/var/lib/nomon/pairing_secret` (mode `0640`, owner `root:nomon`)
- [x] nomopractic reads this file for BLE pairing verification
- [x] Secret rotation: regenerate on daemon restart (existing behaviour);
      overwrite shared file atomically
- [x] Systemd: ensure `/var/lib/nomon/` directory exists with correct
      ownership in `nomothetic-api.service` `ExecStartPre`
- [x] Tests: verify file write, permissions, atomic overwrite

#### 18.2 — JWT Secret Sharing
- [x] Document that `NOMON_JWT_SECRET` env var must be available to BOTH
      nomopractic and nomothetic processes (already in shared env file
      `/etc/nomothetic/nomothetic.env`)
- [x] Verify: JWT issued by nomopractic over BLE is accepted by nomothetic's
      `jwt_required` dependency (same secret, same issuer `nomon-device`)
- [x] Integration test: BLE-issued JWT → HTTPS request → nomothetic validates

#### 18.3 — Documentation Updates
- [x] `docs/pi_setup.md`: add BlueZ prerequisites section
  - `sudo apt install -y bluez` (usually pre-installed on Pi OS)
  - `sudo systemctl enable --now bluetooth`
  - Verify: `bluetoothctl show` shows controller
- [x] `docs/getting_started.md`: add BLE verification step
  - `bluetoothctl show | grep Powered` should show `yes`
  - Note: BLE and WiFi share antenna on Pi Zero 2W
- [x] `docs/hat_ipc_schema.md`: add note that BLE uses a separate binary
      protocol (reference nomopractic ADR-002), not NDJSON

#### Phase 18 Exit Criteria
- [x] Shared pairing secret file written by nomothetic, readable by nomopractic
- [x] JWT issued by nomopractic over BLE is valid for nomothetic HTTPS endpoints
- [x] BlueZ prerequisites documented in pi_setup.md
- [x] All existing tests pass (no regressions)
- [x] `uv run pytest tests/` — ≥ 497 passing
- [x] `uv run ruff check . && uv run black --check .` — clean

---

### Phase 18.1 — BLE Simplification Coordination ⊘ Superseded by Phase 20

**Goal:** Update nomothetic documentation and pairing secret lifecycle to
coordinate with the BLE simplification in nomopractic Phase 13.1 and
nomotactic Phase 2.1. nomothetic is NOT in the BLE data path — its role is
maintaining the shared pairing secret and documenting the IPC contract.

**Architecture decisions:**
- nomopractic ADR-004: BLE Simplification — Native OS Pairing + JSON Relay

**Cross-repo dependencies:**
- nomopractic Phase 13.1: new IPC methods, simplified GATT
- nomotactic Phase 2.1: simplified BLE client

#### 18.1.1 — Pairing Secret Format Change
- [ ] `nomothetic/pairing.py`: change `PairingState.generate_secret()` to
      generate a 6-digit numeric passkey (000000–999999) instead of a
      `secrets.token_urlsafe` string
- [ ] File format: plain text, 6 ASCII digits, no trailing newline
- [ ] Update `verify_and_consume()` — numeric passkey is **not** single-use
      (OS bonding is persistent; passkey is reused for re-pairing)
- [ ] Update startup log: `info!("BLE passkey: %s", passkey)` (operator-visible)
- [ ] Tests: verify 6-digit format, file permissions, overwrite behaviour

#### 18.1.2 — IPC Schema Documentation
- [ ] `docs/hat_ipc_schema.md`: add `authenticate` method spec:
      - Params: `{}`
      - Result: `{ jwt: string, expires_in: integer }`
      - Error: `BLE_ONLY` (called from Unix socket), `NOT_READY` (no JWT secret)
- [ ] `docs/hat_ipc_schema.md`: add `wifi_scan` method spec:
      - Params: `{}`
      - Result: `{ networks: [{ ssid: string, signal: integer, security: string }] }`
      - Error: `HARDWARE_ERROR`
- [ ] `docs/hat_ipc_schema.md`: add `wifi_connect` method spec:
      - Params: `{ ssid: string, password: string }`
      - Result: `{ success: boolean }`
      - Error: `INVALID_PARAMS`, `HARDWARE_ERROR`
- [ ] `docs/hat_ipc_schema.md`: add `wifi_status` method spec:
      - Params: `{}`
      - Result: `{ state: string, ssid: string | null, signal: integer | null }`
      - Error: `HARDWARE_ERROR`
- [ ] `docs/hat_ipc_schema.md`: add `BLE_ONLY` to error code table
- [ ] `docs/hat_ipc_schema.md`: update BLE note — replace binary protocol
      reference with NDJSON relay description (reference ADR-004)
- [ ] `docs/hat_ipc_schema.md`: update method count in header (35 → 39)

#### 18.1.3 — Architecture & Roadmap Updates
- [ ] `docs/architecture.md`: update BLE coordination section — reference
      NDJSON relay instead of binary protocol
- [ ] `docs/roadmap.md`: update Phase 18 description — note that the binary
      protocol coordination is superseded by NDJSON relay (ADR-004)

#### Phase 18.1 Exit Criteria
- [ ] Pairing secret file contains 6-digit numeric passkey
- [ ] `hat_ipc_schema.md` documents all 4 new IPC methods
- [ ] `BLE_ONLY` error code documented
- [ ] BLE note in IPC schema updated to reference NDJSON relay
- [ ] All existing tests pass (no regressions)
- [ ] `uv run pytest tests/` — ≥ 532 passing
- [ ] `uv run ruff check . && uv run black --check .` — clean

---

### Phase 19 — Service Env-File Hardening ✅

**Goal:** Fix two bugs in `scripts/deploy.sh`:
(1) `copy_nomothetic_env()` copies the full project `.env` — including
deploy-machine credentials (`NOMON_PI_HOST`, `NOMON_SSH_KEY`,
`NOMON_REMOTE_DIR`) — to `/etc/nomothetic/nomothetic.env` on the Pi.
(2) The systemd service file installation uses plain `cp`, which does not
expand the `${NOMON_SERVICE_USER}` / `${NOMON_SERVICE_GROUP}` template vars
in `User=` and `Group=`, causing all three services to fail to start.

**Dependency:** None. No Python code changes. No IPC changes.
**Cross-repo:** Paired with nomopractic Phase 14 (same pattern, independent fix).

---

#### 19.1 — Filter Deploy Secrets from Runtime Env File

**File:** `nomothetic/scripts/deploy.sh`

The `copy_nomothetic_env()` function (around line 150) currently pipes the
raw `${ENV_FILE}` to the Pi. Replace it with a filtered version that strips
all deploy-only variables before writing. Also add `_DEPLOY_EXCLUDE` above
the function.

**Remove** the current function body and **replace** with:

```bash
# Variables excluded from the Pi's system env file — deploy secrets only, never runtime config.
_DEPLOY_EXCLUDE='^\s*(NOMON_PI_HOST|NOMON_SSH_KEY|NOMON_REMOTE_DIR|NOMON_GITHUB_REPO)\s*='

copy_nomothetic_env() {
    if [[ ! -f "${ENV_FILE}" ]]; then
        echo "==> Warning: .env not found; skipping /etc/nomothetic/nomothetic.env creation." >&2
        return
    fi

    if [[ -n "${PI_HOST}" ]]; then
        echo "==> Creating /etc/nomothetic/nomothetic.env on remote host..."
        grep -vE "${_DEPLOY_EXCLUDE}" "${ENV_FILE}" | \
            ssh "${SSH_OPTS[@]}" "${PI_HOST}" \
                'sudo mkdir -p /etc/nomothetic && sudo tee /etc/nomothetic/nomothetic.env >/dev/null'
    else
        echo "==> Creating /etc/nomothetic/nomothetic.env locally..."
        sudo mkdir -p /etc/nomothetic
        grep -vE "${_DEPLOY_EXCLUDE}" "${ENV_FILE}" | sudo tee /etc/nomothetic/nomothetic.env >/dev/null
    fi
}
```

**Verify:** After deploy,
`grep -E 'NOMON_PI_HOST|NOMON_SSH_KEY|NOMON_REMOTE_DIR' /etc/nomothetic/nomothetic.env`
returns no output.

---

#### 19.2 — Use `envsubst` for Service File Installation

**File:** `nomothetic/scripts/deploy.sh`, inside the `END_REMOTE` heredoc,
in the `if command -v systemctl >/dev/null 2>&1; then` block (around line 400).

The current loop uses `sudo cp "${_svc_file}" "${_dest}"`, which writes the
literal template placeholders to disk. systemd cannot expand `${...}` vars in
`User=` or `Group=` directives, so the service fails to start.

**Replace** the `_systemd_changed=false` line and the `for` loop with:

```bash
    _systemd_changed=false

    # Source the runtime env file so envsubst can expand User= and Group= template vars.
    NOMON_SERVICE_USER="nomon"
    NOMON_SERVICE_GROUP="nomon"
    if [[ -f /etc/nomothetic/nomothetic.env ]]; then
        set -o allexport
        source /etc/nomothetic/nomothetic.env
        set +o allexport
    fi
    NOMON_SERVICE_USER="${NOMON_SERVICE_USER:-nomon}"
    NOMON_SERVICE_GROUP="${NOMON_SERVICE_GROUP:-nomon}"

    for _svc_file in systemd/*.service; do
        [[ -f "${_svc_file}" ]] || continue
        _svc_name="$(basename "${_svc_file}")"
        _dest="/etc/systemd/system/${_svc_name}"

        _expanded="$(envsubst '$NOMON_SERVICE_USER $NOMON_SERVICE_GROUP' < "${_svc_file}")"
        if [[ ! -f "${_dest}" ]] || [[ "${_expanded}" != "$(cat "${_dest}" 2>/dev/null)" ]]; then
            echo "  Installing ${_svc_name}..."
            printf '%s' "${_expanded}" | sudo tee "${_dest}" >/dev/null
            _systemd_changed=true
        fi
    done
```

**Notes:**
- `envsubst '$NOMON_SERVICE_USER $NOMON_SERVICE_GROUP'` — the explicit variable
  list prevents accidentally substituting other `$` patterns in the file.
- `printf '%s'` pipes the envsubst output without adding an extra trailing
  newline (envsubst already preserves the template's final newline).
- The `env file sourced → defaults applied` pattern ensures that even a
  fresh install with no `.env` (and thus no `nomothetic.env`) still produces
  a valid service file with `User=nomon` / `Group=nomon`.

---

#### Phase 19 Exit Criteria

- `copy_nomothetic_env()` uses `grep -vE "${_DEPLOY_EXCLUDE}"` before writing to the Pi
- `_DEPLOY_EXCLUDE` covers `NOMON_PI_HOST`, `NOMON_SSH_KEY`, `NOMON_REMOTE_DIR`,
  `NOMON_GITHUB_REPO`
- Service file installation loop uses
  `envsubst '$NOMON_SERVICE_USER $NOMON_SERVICE_GROUP'` with env file sourced
  before the loop; defaults of `nomon`/`nomon` applied when env file absent
- After deploy:
  - `grep -E 'NOMON_PI_HOST|NOMON_SSH_KEY|NOMON_REMOTE_DIR' /etc/nomothetic/nomothetic.env`
    returns no output
  - `grep -E '^User=|^Group=' /etc/systemd/system/nomothetic-api.service`
    returns `User=nomon` and `Group=nomon` (literal values, not template vars)
  - `systemctl is-active nomothetic-api` → `active`
  - `systemctl is-active nomothetic-stream` → `active` (if enabled)
- `pytest && ruff check . && black --check .` — clean (no Python code changes)

---

### Phase 20 — BLE → Wi-Fi Soft AP Migration ✅

**Goal**: Remove all BLE coordination from nomothetic; update documentation to
reflect the Wi-Fi Soft AP pairing channel introduced in nomopractic Phase 15.
nomothetic is NOT in the BLE data path — its role in Phases 18/18.1 was
managing the shared pairing secret and documenting BLE prerequisites. With BLE
removed, the shared pairing secret is still written to
`/var/lib/nomon/pairing_secret` (now dual-purpose: startup display + Soft AP
WPA2 password); only the BLE framing in docs and code comments changes.

**Supersedes**: Phase 18 (BLE Pairing Coordination), Phase 18.1 (BLE
Simplification Coordination)
**Cross-repo**: nomopractic Phase 15

#### 20.1 — Update Pairing Module

- [x] `src/nomothetic/pairing.py`: update module docstring — remove reference to
      nomopractic BLE pairing; replace with note that the shared file is also
      used as the Soft AP WPA2 password. The `_write_shared_secret()` function
      and its logic are **unchanged**.
- [x] `src/nomothetic/pairing.py`: update `_write_shared_secret` inline comment
      that says "only BLE pairing requires the shared file" — change to "the
      shared file is required by the nomon-softap watchdog script as the WPA2
      password for the Soft AP hotspot"
- Verify: `pytest tests/test_pairing.py` — all 14 tests pass (no logic changes)

#### 20.2 — Update IPC Schema

- [x] `docs/hat_ipc_schema.md`: remove the BLE transport note at the top of the
      document (the `> **BLE note:** …` block)
- [x] `docs/hat_ipc_schema.md`: remove the `### wifi_scan`, `### wifi_connect`,
      `### wifi_status`, and `### authenticate` method sections entirely
      (these IPC methods are deleted in nomopractic Phase 15.4)
- [x] `docs/hat_ipc_schema.md`: remove `BLE_ONLY` from the error code table if
      present
- Verify: `grep -n 'BLE\|wifi_scan\|wifi_connect\|wifi_status\|authenticate\|BLE_ONLY' docs/hat_ipc_schema.md` — no output

#### 20.3 — Update Architecture Doc

- [x] `docs/architecture.md`: replace the BLE coordination section with a
      Wi-Fi Soft AP section that describes:
  - Soft AP managed by `nomon-softap-watchdog` systemd timer (nomopractic Phase 15)
  - SSID/password derivation from `/var/lib/nomon/pairing_secret`
  - How nomothetic's HTTP stack is reachable at `192.168.4.1:8080` when the
    AP is active (plain HTTP, interface-bound; HTTPS/TOFU approach later
    adopted in Phase 22 then reverted — see ADR-016)
  - The existing `POST /api/device/auth/pair` endpoint serves AP-mode clients
    identically to normal Wi-Fi clients
- Verify: `grep -n 'BLE\|bluer\|bluetooth\|Bluetooth' docs/architecture.md` —
      no substantive references remain

#### Phase 20 Exit Criteria

- [x] `pytest && ruff check . && black --check .` — all clean
- [x] `grep -rn 'BLE\|bluer\|bluetooth' src/nomothetic/pairing.py` — no output
- [x] `docs/hat_ipc_schema.md` contains no `wifi_scan`, `wifi_connect`,
      `wifi_status`, or `authenticate` sections
- [x] `docs/architecture.md` BLE section replaced with Soft AP description

### Phase 20.4 — Wi-Fi Credential Provisioning ✅

Adds the missing provisioning step to the Soft AP pairing flow. The watchdog
already handled AP teardown on full connectivity; this phase adds the API
endpoint, Pydantic models, rate limiter, and UI form that let the user
supply home Wi-Fi credentials.

- [x] `WifiProvisionRequest` / `WifiProvisionResponse` Pydantic models in `api.py`
- [x] `network_rate_limit` dependency (5 req / 60 s per IP) in `rate_limit.py`
- [x] `POST /api/device/network/configure` endpoint — fires `nmcli` as asyncio background task, returns `{"status": "connecting"}` immediately
- [x] `app.state.network_limiter` initialised in both device-auth-enabled and disabled branches
- [x] 8 pytest cases in `tests/test_network_provision.py`
- [x] `scripts/deploy.sh` — adds `nomon` user to `netdev` group
- [x] `docs/pi_setup.md` — NetworkManager group access subsection
- [x] nomotactic: `WifiProvisionForm` component rendered inline after pairing

#### Phase 20.4 Exit Criteria

- [x] `pytest && ruff check . && black --check .` — all clean
- [x] `POST /api/device/network/configure` returns `{"status":"connecting"}` for valid JWT + SSID + password
- [x] `422` for SSID > 32 chars, empty SSID, or password 1–7 chars
- [x] `429` after 5 requests within 60 s
- [x] `401` without Authorization header
- [x] `WifiProvisionForm` renders inline after successful pairing, no new screens or dependencies

**Cross-repo:** nomopractic Phase 15.8

---

### Phase 20.5 — Wi-Fi AP Mode Toggle ✅

Adds a manual AP mode control endpoint so the app (or operator) can explicitly
bring the Soft AP up or down without waiting for the watchdog.

- [x] `POST /api/device/wifi/ap` — toggle Soft AP on/off
  Request: `{ "subcommand": "up" | "down" }`, Response: `{ "subcommand", "timestamp" }`
  Invokes `ap-mode.sh <subcommand>` via `subprocess.run` in a thread-pool executor
  Script path from `NOMON_AP_MODE_SCRIPT` env var (default: `/opt/nomon/scripts/ap-mode.sh`)
  Subcommand validated against `{"up", "down"}` allowlist — not passed verbatim from user input
  `422` on invalid subcommand; `500` on script failure

---

### Mobile & Web App (nomotactic)

Developed in the `nomotactic` repository. Expo (React Native) app serving
Android, iOS, and web from a single TypeScript codebase.

**Interfaces consumed:**
- Device mode HTTPS API: `https://<pi-tailscale-ip>:8443` (device control)
- Central mode HTTPS API: `https://<central-host>/api/auth/*`, `/api/fleet/*`
  (authentication, fleet management)
- Self-signed cert acceptance for device connections

**See:** `nomotactic/docs/roadmap.md`, `nomotactic/docs/architecture.md`

### Database (nomographic)

Managed in the `nomographic` repository. ArcadeDB schemas and ArcadeDB-native migrations.

**Interfaces:**
- Central mode: nomothetic connects to ArcadeDB server via HTTP API
- Local mode: nomothetic opens embedded ArcadeDB from filesystem

**See:** `nomographic/docs/roadmap.md`, `nomographic/docs/architecture.md`

---

### Phase 21 — HTTP AP Pairing Service ✅

Decouples the Soft AP pairing channel from the main HTTPS API so that
Tailscale-issued (Let's Encrypt-backed) certificates can be used on the primary
interface without breaking AP-mode pairing. See **ADR-015**.

- [x] `systemd/nomothetic-ap.service` — plain HTTP on port 8080 (`0.0.0.0`)
  bound alongside the existing `nomothetic-api.service` (HTTPS, port 8443).
- [x] `scripts/deploy.sh` — stop, enable, and restart `nomothetic-ap.service`
  in the systemd service lifecycle alongside `nomothetic-api` and
  `nomothetic-stream`; rollback handler covers the AP service.
- [x] `docs/adr/015-http-for-ap-mode.md` — ADR documenting the decision,
  security risks accepted (R1–R4), and future mitigation path (nftables,
  targeted Android network security config).

---

### Phase 22 — Clean AP/WiFi Mode Separation ✅

**Goal:** Fix the two outstanding ADR-015 issues: (1) bind `nomothetic-ap.service`
to `192.168.4.1` only (not `0.0.0.0`) to prevent LAN exposure; (2) persist the
JWT signing secret across AP → WiFi mode switches so re-pairing is not required.
Also: clean Python module boundary for AP-mode code.
ADR-016 initially adopted HTTPS+TOFU but was subsequently amended (2026-05-11)
back to plain HTTP — see ADR-016 for full rationale.

**Architecture decisions:** ADR-016 (amended)

**Cross-repo dependencies:** nomotactic — `SOFT_AP_URL` update, Expo config plugin.

---

#### 22.1 — AP Certificate Module (removed)

> **Note (2026-05-11):** This sub-phase is removed.  The original ADR-016 design
> included a dedicated self-signed certificate module (`nomothetic.ap_mode.cert`)
> for the AP HTTPS service.  This module (`cert.py`, `__init__.py`) was deleted
> when ADR-016 was amended to revert to plain HTTP — no TLS certificate is needed
> for the AP HTTP service on port 8080.

- [~] ~~New package `src/nomothetic/ap_mode/` with `__init__.py`~~ — deleted
- [~] ~~`src/nomothetic/ap_mode/cert.py`~~ — deleted

#### 22.2 — AP Bootstrap Service (removed)

> **Note (2026-05-11):** This sub-phase is removed.  The original ADR-016
> design included a separate HTTP bootstrap service (`nomothetic-ap-bootstrap.service`
> on port 8080) to deliver the AP self-signed cert PEM for TOFU pinning.  This
> service and the associated `nomothetic.ap_mode.bootstrap` module were deleted
> when ADR-016 was amended to revert to plain HTTP (the main AP service is now
> itself on HTTP port 8080 bound to `192.168.4.1`).

- [~] ~~`src/nomothetic/ap_mode/bootstrap.py`~~ — deleted
- [~] ~~`systemd/nomothetic-ap-bootstrap.service`~~ — deleted
- [~] ~~`nomopractic/scripts/ap-mode.sh` bootstrap start/stop~~ — removed

#### 22.3 — AP Main Server (binding fix)

- [x] Update `systemd/nomothetic-ap.service` — change `ExecStart` from:
  ```
  uvicorn nomothetic.api:create_app --factory --host 0.0.0.0 --port 8080
  ```
  to:
  ```
  uvicorn nomothetic.api:create_app --factory \
    --host 192.168.4.1 --port 8080
  ```
  No SSL flags, no `ExecStartPre` (cert generation removed in amendment).
- [x] Verify: `ss -tlnp | grep 8080` shows `192.168.4.1:8080` only.

#### 22.4 — Persisted Device JWT Secret (`nomothetic.device_jwt`)

- [x] `src/nomothetic/device_jwt.py`:
  - `_DEFAULT_SECRET_PATH = "/var/lib/nomon/device_jwt_secret"`
    (env: `NOMON_DEVICE_JWT_SECRET_PATH`)
  - `DeviceJwtSecretStore` class:
    - `load_or_generate() -> str` — read from file (if `≥ 32` chars); otherwise
      generate `secrets.token_urlsafe(48)`, write atomically with
      `tempfile.mkstemp` + `os.rename`, set `0600 nomon:nomon` (same pattern as
      `pairing.py._write_shared_secret()`); return the secret
    - `rotate() -> str` — always generate a new secret, overwrite file, return it
    - `_read() -> str | None` — internal; reads file, returns `None` on any error
      or if value `< 32` chars (corrupt file guard)
    - `_write(secret: str) -> None` — internal; atomic write with `0600` permissions
    - Logs `INFO` on read, `INFO` on generate+write, `WARNING` on write failure
      (service continues with in-memory secret if `/var/lib/nomon/` absent)
- [x] Modify `src/nomothetic/pairing.py` (doc-comment change + call site only):
  - `PairingState.__init__()`: update `# …` comment noting that `jwt_secret` is
    now loaded from `DeviceJwtSecretStore.load_or_generate()`; the call site
    `self.jwt_secret = secrets.token_urlsafe(48)` is replaced by
    `self.jwt_secret = DeviceJwtSecretStore().load_or_generate()`
  - `PairingState.reset()`: update comment noting `jwt_secret` is rotated via
    `DeviceJwtSecretStore().rotate()`
- [x] Verify: start `nomothetic-ap.service`, pair, record JWT; stop and start
  `nomothetic-api.service`; existing JWT is still accepted (same secret on disk).
  Delete `/var/lib/nomon/device_jwt_secret`, restart — JWT is rejected (new secret).

#### 22.5 — Configuration

- [x] `config.toml` — add `[ap_mode]` section:
  ```toml
  [ap_mode]
  # Directory for the AP-specific self-signed certificate.
  # Override with NOMON_AP_CERT_DIR env var.
  cert_dir = "/var/lib/nomon/ap-certs"
  ```
- [x] Update `scripts/start.sh` — export `NOMON_AP_CERT_DIR` from `[ap_mode].cert_dir`
  (matching the pattern used for other `[section].key` → env var exports)

#### 22.6 — Tests (removed)

> **Note (2026-05-11):** This sub-phase is removed.  `tests/test_ap_mode.py` was
> deleted in the ADR-016 amendment along with the `nomothetic.ap_mode` package it
> tested (cert generation, bootstrap service, AP server binding).  The
> `DeviceJwtSecretStore` tests in this file were also removed; `DeviceJwtSecretStore`
> itself is retained in `nomothetic.device_jwt` and is tested via integration paths.

- [~] ~~`tests/test_ap_mode.py`~~ — deleted (all 12 tests removed)
- [x] `uv run pytest tests/` — no regressions (≥ 532 passing)

#### 22.7 — nomotactic Changes

**Note (2026-05-11):** The `react-native-ssl-pinning` dependency and `apFetch.ts`
TOFU wrapper have been removed.  AP mode now uses standard `fetch` over plain HTTP.
See ADR-016 amendment.

- [x] `nomotactic/constants/config.ts`:
  - `SOFT_AP_URL = "http://192.168.4.1:8080"` (plain HTTP; was `https://...8443`)
  - `SOFT_AP_BOOTSTRAP_URL` removed (was `http://192.168.4.1:8080` — same address)
  - Comment updated to reference ADR-016
- [~] ~~`nomotactic/lib/apFetch.ts`~~ — deleted (TOFU wrapper; no longer needed)
- [x] `nomotactic/lib/auth.tsx`:
  - `connectToAp()` simplified: sets `deviceBaseUrl(SOFT_AP_URL)` only
  - `pairViaAp()` uses standard `fetch` instead of `fetchWithApCert`
  - All `apFetch` and `SOFT_AP_BOOTSTRAP_URL` imports removed
- [x] `nomotactic/app.json`:
  - Android: `usesCleartextTraffic: true` removed; Expo config plugin retained
    (cleartext scoped to `192.168.4.1` only)
  - iOS: `NSExceptionAllowsInsecureHTTPLoads: true` scoped to `192.168.4.1`
    retained (already in place)
- [x] `nomotactic/plugins/apModeTlsPlugin.ts` — retained; comments updated to
  remove TOFU/pinning references; cleartext exception for `192.168.4.1` unchanged
- [~] ~~`react-native-ssl-pinning`~~ — uninstalled from `package.json`
- [x] Verify: `npx expo lint` — 0 errors; `npx tsc --noEmit` — 0 errors

#### 22.8 — Documentation and ADR Updates

- [x] `docs/adr/016-ap-mode-https.md` — new ADR (already created)
- [x] `docs/adr/015-http-for-ap-mode.md` — status updated to `Superseded by ADR-016`
- [x] `docs/adr/001-self-signed-tls-certs.md` — add note in "Future" section:
  "The AP mode now has its own dedicated self-signed cert at `/var/lib/nomon/ap-certs/`,
  generated by `nomothetic.ap_mode.cert`; this is separate from the WiFi cert
  managed by `provision_tls_cert()` (ADR-016)."
- [x] `docs/adr/014-device-mode-auth.md` — add note documenting JWT secret
  persistence via `DeviceJwtSecretStore`; update "Auto-Generated JWT Secret" section
  to describe the new persistence behaviour and the `/var/lib/nomon/device_jwt_secret`
  file (`0600 nomon:nomon`)
- [x] `docs/architecture.md` — update "Wi-Fi Soft AP note" and provisioning sequence
  to reflect HTTPS on port 8443 and the TOFU bootstrap step on port 8080
- [x] `docs/roadmap.md` — Phase 22 added (this entry)

#### Phase 22 Exit Criteria

- [x] `sudo systemctl start nomothetic-ap` (with AP active):
  - `curl http://192.168.4.1:8080/` returns `{ "status": "ok" }`
- [x] AP service bound only to `192.168.4.1`: `ss -tlnp | grep 8080` shows `192.168.4.1:8080` only
- [x] JWT survives AP → WiFi transition (manual test: pair on AP, switch to WiFi, use JWT)
- [x] Delete `/var/lib/nomon/device_jwt_secret`, restart service → JWT rejected (new secret)
- [x] `uv run pytest tests/` — ≥ 591 passing
- [x] `uv run ruff check src/ tests/` — 0 errors
- [x] `uv run black --check src/ tests/` — clean
- [x] `uv run mypy src/ tests/` — 0 errors
- [x] `npx expo lint` (nomotactic) — 0 errors
- [x] `npx tsc --noEmit` (nomotactic) — 0 errors

---

### Phase 23 — Device Fleet Registration & Identity

**Goal:** Enable a paired user to register their physical device with the
central fleet API via a proof-of-access token flow. The device issues a
short-lived registration proof JWT; the central API validates it structurally
before creating the fleet record.

**Architecture decisions:**
- ADR-017: Device Registration Proof JWT
- ADR-018: Web Token Storage Strategy

**Cross-repo dependencies:**
- nomotactic: `DeviceRegistrationForm` component, `GET /api/device/auth/identity` relay
- nomothetic (central): `POST /api/fleet/devices` updated to require `registration_proof`

#### 23.1 — Device Identity Endpoint (`nomothetic.device_auth_routes`)

- [x] `DeviceIdentityResponse` Pydantic model: `vin`, `model`, `hostname`,
      `registration_proof`
- [x] `GET /api/device/auth/identity` — requires device JWT (pairing_rate_limit:
      3 req/min)
  - Calls `_derive_vin()` for VIN resolution
  - Generates `registration_proof` JWT: HS256, 5-min TTL, claims
    `iss=nomon-device`, `sub=<vin>`, `aud=nomon-fleet`, unique `jti`
  - Returns `{ vin, model, hostname, registration_proof }`
- [x] `NOMON_VIN` environment variable renamed to `NOMON_DEVICE_ID`
  - **Breaking change:** operators must update `.env` / systemd override files

#### 23.2 — Fleet Registration Proof Validation (`nomothetic.fleet_routes`)

- [x] `POST /api/fleet/devices` request body updated: `{ vin, model,
      registration_proof }` (requires central JWT)
  - `registration_proof` is validated: `exp`, `sub == vin`, `aud == "nomon-fleet"`
  - Cryptographic signature verification deferred (device and central use
    separate JWT secrets — see ADR-017)
  - `400` on invalid or expired proof; `422` on missing field

#### 23.3 — nomotactic: Device Registration Form

- [x] `components/DeviceRegistrationForm.tsx` — handles central fleet
      registration for users with no registered devices; discovery-driven
      flow (direct / ap / needs-pairing); calls `GET /api/device/auth/identity`
      on the device then relays the proof to `POST /api/fleet/devices` on
      the central API

#### 23.4 — Web Token Storage Hardening (nomotactic)

- [x] Access tokens (central + device): memory-only — stored in React state,
      never written to browser storage
- [x] Refresh tokens (central + device): `sessionStorage` — tab-scoped,
      cleared on tab/window close
- [x] Device URL: `localStorage` — non-sensitive, persists across sessions
- [x] Mobile: unchanged — `expo-secure-store` for all tokens

#### Phase 23 Exit Criteria

- [x] `GET /api/device/auth/identity` (with valid device JWT) returns `vin`,
      `model`, `hostname`, `registration_proof`
- [x] `POST /api/fleet/devices` accepts `{ vin, model, registration_proof }`
      and rejects expired or mismatched proofs with `400`
- [x] `NOMON_DEVICE_ID` env var resolves the VIN correctly (replaces `NOMON_VIN`)
- [x] `DeviceRegistrationForm` completes the end-to-end registration flow
- [x] Web token storage: access tokens are not written to `localStorage` or
      `sessionStorage`; refresh tokens are in `sessionStorage` only
- [x] `uv run pytest tests/` — no regressions (≥ 591 passing)
- [x] `uv run ruff check src/ tests/` — 0 errors
- [x] `uv run black --check src/ tests/` — clean
- [x] `npx expo lint` (nomotactic) — 0 errors
- [x] `npx tsc --noEmit` (nomotactic) — 0 errors

---

### Phase 24 — Autonomy Routine Launcher (autonomon plugin handoff) ✅

**Goal:** Let the device launch, supervise, and report on `autonomon` autonomy
routines without nomothetic ever importing the brain or performing any
cognition. nomothetic is a thin process supervisor and telemetry sink; all
perception, world-modelling, and planning stays in the launched `autonomon`
process (autonomon ADR-004). The two projects keep separate venvs and hand off
through a file-based catalogue (autonomon ADR-005).

> **Naming note:** this **autonomy** routine launcher (`/api/routines/*`, plural)
> is distinct from the firmware **HAT** routine API (`/api/routine/*`, singular,
> Phase 11) that drives nomopractic's in-daemon `explore`. Same word, different
> execution model — see the Phase 6 naming note in autonomon's roadmap.

**Cross-repo dependencies:**
- autonomon: publishes its catalogue (`nomon_manifest` + `nomon-autonomon` CLI
  path) to `NOMON_ROUTINE_CATALOG_PATH`; the launched plugin connects back to
  this device's REST API and reports lifecycle events.
- nomothetic ADR-019 (plugin challenge-response auth) issues the device JWT the
  plugin uses; this phase is the launcher/supervisor that sits on top of it.

#### 24.1 — Catalogue Reader (`nomothetic.routine_catalog`)
- [x] Reads the JSON catalogue autonomon publishes (routine names, param
      schemas, version, absolute `nomon-autonomon` path) from
      `NOMON_ROUTINE_CATALOG_PATH` (default `/var/lib/nomon/routine_catalog.json`)
- [x] Missing / unreadable / malformed file → empty catalogue (no routines),
      not an error — a device with no autonomon deployed simply offers none
- [x] 10 tests (`tests/test_routine_catalog.py`)

#### 24.2 — Process Supervisor (`nomothetic.routine_manager`)
- [x] `RoutineManager` spawns one `nomon-autonomon` subprocess per routine;
      device URL, id, and credentials injected from `RoutineManagerConfig` so no
      secret ever travels in the start payload
- [x] **Heartbeat lease** — each routine runs under a renewable lease: every
      `POST /api/routines/heartbeat` pushes the deadline out by
      `heartbeat_timeout_s`; if heartbeats stop (operator lost contact), a
      per-process watchdog stops the routine. Optional absolute `max_duration_s`
      caps total runtime regardless of heartbeats
- [x] Coarse process-level safety net complementing nomopractic's fine-grained
      actuator-lease watchdog (motors idle within one TTL when the plugin stops
      commanding)
- [x] 29 tests (`tests/test_routine_manager.py`)

#### 24.3 — Lifecycle Control & Status/Log Sink (`routine_control_routes`, `routine_routes`, `routine_log_store`)
- [x] Control endpoints: `GET /api/routines/available`, `POST /api/routines/start`,
      `POST /api/routines/heartbeat`, `POST /api/routines/stop`,
      `POST /api/routines/stop-all`
- [x] Status/log sink (push model): the brain reports its own
      `starting`/`running`/`stopping`/`error` + free-form `log` events;
      `routine_log_store` keeps a bounded per-routine ring buffer segmented by
      `run_id`; served via `GET /api/routines` and `GET /api/routines/{routine}/logs`
- [x] Operational telemetry only — nomothetic stores and returns exactly what the
      brain reports and derives a coarse status from the event type (ADR-004)
- [x] 17 tests (`tests/test_routine_control.py`) + 16 tests (`tests/test_routine_logs.py`)

#### Phase 24 Exit Criteria
- [x] `GET /api/routines/available` lists routines from the published catalogue;
      empty when autonomon has published none
- [x] `POST /api/routines/start` launches a routine as a supervised subprocess;
      heartbeats keep it alive; lapsed lease or `max_duration_s` stops it
- [x] Lifecycle events from the running plugin are queryable per routine
- [x] nomothetic never imports autonomon (separate venvs; file-based handoff)
- [x] 72 new tests (10 catalogue + 17 control + 16 logs + 29 manager)
- [x] `uv run ruff check src/ tests/`, `black --check`, `mypy` — clean

---

### Phase 25 — Fleet Telemetry History + Profile Editing ✅

**Goal:** Give the central API the two pieces nomotactic's Fleet Management
Dashboard (nomotactic Phase 4) needed but that did not exist: a persisted
telemetry **history** (telemetry was MQTT-only; `latest_telemetry` was hardcoded
`null`) and **profile-edit** endpoints (display-name update + password change).

**Cross-repo dependencies:**
- nomographic central V1 already defines the `TelemetryReading` vertex
  (`battery_voltage`, `cpu_temp_c`, `uptime_seconds`, `recorded_at`) and the
  `ReadFrom` edge — this phase consumes that schema; no migration needed.
- nomotactic Phase 4 consumes the new endpoints.

#### 25.1 — Telemetry Store (`nomothetic.telemetry_store`)
- [x] `TelemetryReadingItem` model + `TelemetryStore` Protocol with
      `InMemoryTelemetryStore` (bounded per-VIN ring) and `SqlTelemetryStore`
      (inserts a `TelemetryReading` and links it to the `Vehicle` via a
      `ReadFrom` edge; history via `Vehicle.in('ReadFrom') ORDER BY recorded_at`).
      Mirrors the `fleet_store.py` pattern.
- [x] Methods: `record_reading`, `get_history(limit, since)`, `get_latest`.

#### 25.2 — MQTT Ingestion (`nomothetic.telemetry_consumer`)
- [x] Central-mode background MQTT subscriber (reuses the existing `paho-mqtt`
      dep) that consumes `NOMON_MQTT_TOPIC` (`nomon/telemetry`), maps
      `device_id` → VIN, and persists readings. The broker is the device→central
      transport, so **no** new device-authenticated REST ingestion endpoint is
      introduced (which would re-open the deferred device→central auth design,
      autonomon Phase 7). No broker configured → no consumer, history just empty.
- [x] `reading_from_payload` is a pure function (unit-testable without a broker);
      `TelemetryConsumer.ingest` is an awaitable scheduled on the API event loop.
- [x] Device payload enriched: `TelemetryPublisher.build_payload()` now adds
      `battery_voltage` (via `HatClient`, best-effort), `cpu_temp_c` (sysfs), and
      `uptime_seconds` (`/proc/uptime`).

#### 25.3 — Fleet Telemetry Routes (`nomothetic.fleet_routes`)
- [x] `GET /api/fleet/devices/{vin}/telemetry?limit=&since=` → ordered readings
      (central JWT, ownership-scoped via the existing `get_device` guard).
- [x] `GET /api/fleet/devices/{vin}` now populates `latest_telemetry` from the
      telemetry store (was always `null`).
- [x] Wired into `create_app()`: `SqlTelemetryStore` when `ARCADEDB_HOST` is set,
      else `InMemoryTelemetryStore`; consumer started/stopped in the lifespan.

#### 25.4 — Profile Editing (`nomothetic.auth_routes` / `auth` / `user_store`)
- [x] `PATCH /api/auth/me` `{ display_name }` → updated `UserResponse`
      (`AuthService.update_display_name` → `UserStore.update_user`, which already
      whitelists `display_name`).
- [x] `POST /api/auth/change-password` `{ current_password, new_password }` →
      `{ success }`. `AuthService.change_password` verifies the current password
      (bcrypt), writes the new hash via a dedicated `UserStore.set_password_hash`
      (kept off the general update whitelist — credential mutation on its own
      path), and revokes all of the user's refresh tokens.

#### Phase 25 Exit Criteria
- [x] Telemetry published by a device is persisted and queryable as history;
      `latest_telemetry` is populated on device detail.
- [x] `PATCH /api/auth/me` updates the display name; `POST /api/auth/change-password`
      changes the password, rejects a wrong current password (401), enforces the
      8-char minimum (422), and revokes prior refresh tokens.
- [x] In-memory stores by default; ArcadeDB stores when `ARCADEDB_HOST` is set.
- [x] New tests: telemetry store/consumer + history endpoint + profile/password
      (`tests/test_telemetry_store.py`, `tests/test_telemetry_consumer.py`,
      additions to `tests/test_central.py`); `ruff`/`black`/`mypy` clean.

---

### Phase 26 — AI Chat-Command Relay (device mode) ✅

**Goal:** Give nomotactic's command bar (nomotactic Phase 3) a real endpoint: a
device-mode Claude relay that turns operator chat into the **same validated
device operations the app's buttons use**. Operator convenience, not autonomy —
no cognition or robot state lives in nomothetic (the ADR-004 boundary holds;
autonomy stays in autonomon).

**Cross-repo dependencies:**
- nomotactic Phase 3 consumes the endpoints (`CommandInput` → `lib/ai.ts`).

#### 26.1 — Command Service (`nomothetic.ai_command`)
- [x] `AiCommandService` — agentic loop against the Anthropic Messages API
      (`anthropic` SDK via the new optional `[ai]` extra; default model
      `claude-opus-4-8`, adaptive thinking; `NOMON_AI_MODEL`,
      `NOMON_AI_MAX_TOKENS`, `NOMON_AI_MAX_TOOL_ITERATIONS` overrides; capped
      tool round trips per command).
- [x] **Destructive-free tool registry**: drive / steer / camera pan+tilt under
      the same Pydantic bounds and TTL leases as the manual endpoints, `stop`,
      sensor reads (ultrasonic, grayscale, battery, daemon health, lease
      statuses), and routine list/start/stop/stop-all through the existing
      `RoutineManager` lease machinery. Deliberately excluded: `reset_mcu`,
      all calibration writes, raw servo pulses, raw per-motor speeds (pinned
      by test).
- [x] `AiKeyStore` — user-supplied Anthropic key persisted atomically `0600`
      at `/var/lib/nomon/ai_api_key` (`NOMON_AI_API_KEY_PATH` override); a
      stored key wins over the `ANTHROPIC_API_KEY` env fallback; the key is
      never logged and never returned by the API.

#### 26.2 — Routes (`nomothetic.ai_routes`)
- [x] `GET/PUT/DELETE /api/ai/key` — key presence/source metadata only.
- [x] `POST /api/ai/command` — plain-text chat turns in (validated
      user/assistant alternation, ≤ 40 messages), reply + per-action record
      out. Rate limited (10/min/IP, `ai_rate_limit`); provider failures map
      to 502 (an auth-rejected key is distinguishable), missing key or SDK
      to 503. Mounted on the device router → inherits device JWT auth.

#### Phase 26 Exit Criteria
- [x] A chat command executes robot tools through `_hat_call` validation and
      returns the ordered action log alongside the reply.
- [x] Destructive HAT methods are unreachable from the tool surface
      (`test_destructive_hat_methods_not_exposed_as_tools`).
- [x] Key lifecycle covered by tests: stored `0600`, never echoed, stored-over-env
      precedence, format rejection.
- [x] `make check` clean (`ruff`/`black`/`mypy`; 776 tests).

---

### Phase 27 — Autonomy Telemetry Persistence (MQTT device→central) ✅

**Goal:** Persist fleet-wide **autonomy** run history (autonomon Phase 7) so the
nomotactic per-device dashboard can show what a device's autonomy routines did,
not just its hardware telemetry. The device→central transport question that
deferred autonomon Phase 7 is answered the same way Phase 25 answered it for
device telemetry: **MQTT is the transport** — no new device-authenticated
central REST ingestion endpoint (that would re-open the deferred device→central
auth design). nomothetic stores exactly what the brain reports (ADR-004); the
coarse run status is derived from the lifecycle event type, mirroring the
device-local `RoutineLogStore`.

**Cross-repo dependencies:**
- nomographic central `V4__add_autonomy_schema.sql` (`AutonomyRun` +
  `AutonomyEvent` vertices, `PerformedBy` + `PartOf` edges).
- autonomon: **no change** — its existing `StatusReporter` already reports
  lifecycle events (with `run_id` + `device_id`) to the device routine sink.

#### 27.1 — Device-side Event Forwarding (`nomothetic.autonomy_forwarder`)
- [x] `RoutineLogStore` gains an optional `on_event(routine, event)` observer,
      fired (outside the lock, exceptions swallowed) after every recorded event —
      both brain-reported (`/api/routines/{routine}/events`) and
      supervisor-recorded.
- [x] `AutonomyEventForwarder` mirrors each recorded event onto the MQTT autonomy
      topic (`nomon/autonomy`; `NOMON_MQTT_AUTONOMY_TOPIC`). Best-effort: bounded
      queue (drops when full), daemon publish loop, reconnect back-off. Wired as
      the `RoutineLogStore` `on_event` hook and started/stopped in the API
      lifespan; no broker (or no `paho-mqtt`) → forwarding is simply off.

#### 27.2 — Autonomy Store (`nomothetic.autonomy_store`)
- [x] `AutonomyRunItem` / `AutonomyEventItem` models + `AutonomyStore` Protocol
      with `InMemoryAutonomyStore` (bounded per-VIN runs, per-run events) and
      `SqlAutonomyStore` (`AutonomyRun`--`PerformedBy`-->`Vehicle`,
      `AutonomyEvent`--`PartOf`-->`AutonomyRun`; `run_id`+`vin`-scoped). Mirrors
      the `telemetry_store.py` pattern.
- [x] Methods: `record_event` (creates/updates the run as a side effect),
      `get_runs(limit, since)`, `get_events(run_id, limit)`.

#### 27.3 — Central Ingestion + Fleet Routes
- [x] `TelemetryConsumer` also subscribes to the autonomy topic (when an
      `AutonomyStore` is provided) and routes messages by topic;
      `autonomy_event_from_payload` is a pure, unit-tested parser.
- [x] `GET /api/fleet/devices/{vin}/autonomy` (run history) and
      `GET /api/fleet/devices/{vin}/autonomy/{run_id}/events` (one run's events),
      central JWT + ownership-scoped via the existing `get_device` guard.
- [x] Wired into `create_app()`: `SqlAutonomyStore` when `ARCADEDB_HOST` is set,
      else `InMemoryAutonomyStore`; consumer passes it through.

#### Phase 27 Exit Criteria
- [x] Lifecycle events a device records are forwarded to central over MQTT and
      queryable as run history; unknown run/device → empty list, not an error.
- [x] No broker configured → autonomy history stays device-local (no error).
- [x] nomothetic never imports autonomon (file-catalogue + MQTT only).
- [x] New tests: `test_autonomy_store.py`, `test_autonomy_forwarder.py`,
      autonomy cases in `test_telemetry_consumer.py` + `test_central.py`.
- [x] `make check` clean (`ruff`/`black`/`mypy`; 824 tests).

---

### Phase 28 — Voice Command Transcription (on-device STT) ✅

**Goal:** Give the app's AI command bar voice input (the deferred piece of
nomotactic Phase 3). The Anthropic API accepts no audio, so the device
transcribes locally: the app records a short clip, uploads it, and feeds the
returned text to the existing `POST /api/ai/command` path. Like Phase 26 this
is operator convenience, not autonomy — no cognition in nomothetic (ADR-004),
and the robot's own microphone is not involved. See ADR-020.

**Cross-repo dependencies:**
- nomotactic Phase 3 follow-up consumes the endpoint (`CommandInput` mic →
  `lib/voice.ts`).

#### 28.1 — STT Engine (`nomothetic.stt`)
- [x] `SttEngine` Protocol (`transcribe(audio, content_type) -> SttResult`) —
      the pluggable seam for future models or cloud transcription services.
- [x] `VoskSttEngine`: offline Vosk small-English model, **lazy-loaded** on
      first request and **serialized** under the engine lock (512 MB Pi Zero
      2W); model dir from `NOMON_STT_MODEL_PATH`.
- [x] ffmpeg subprocess normalisation to 16 kHz mono PCM — accepts m4a/AAC
      (mobile), webm/Opus (web), wav, anything ffmpeg demuxes.
- [x] Missing vosk (`[stt]` extra), model, or ffmpeg → `SttUnavailableError`
      (HTTP 503), never a crash; undecodable audio → `SttTranscriptionError`.

#### 28.2 — Route (`nomothetic.ai_routes`)
- [x] `POST /api/ai/transcribe` — multipart `audio` upload → `{ text, engine,
      timestamp }`; empty text = silence (a success, not an error). Device JWT
      auth inherited from the device router.
- [x] 503 unavailable / 413 over `NOMON_STT_MAX_BYTES` (default 2 MB) /
      422 empty or undecodable; `stt_limiter` (20/min/IP) separate from
      `ai_limiter` so transcribe + command doesn't double-count.
- [x] Engine injected on `app.state` in `create_app()` (same DI seam as the
      AI service) — tests run against a fake engine.

#### 28.3 — Deployment
- [x] `scripts/fetch_stt_model.sh` + `make fetch-stt-model` — download/unpack
      the Vosk small model to `/var/lib/nomon/stt`, group-readable by `nomon`.
- [x] `docs/pi_setup.md` §5.1 (ffmpeg apt prerequisite, model fetch, verify
      curl); `.env.device.example` STT section.

#### Phase 28 Exit Criteria
- [x] A recorded clip uploaded to `/api/ai/transcribe` returns its transcript;
      the text drives the robot through the existing `/api/ai/command` loop.
- [x] No model/ffmpeg/vosk on the device → 503 with an actionable message; all
      other endpoints unaffected.
- [x] New tests: `tests/test_stt.py` + transcribe cases in
      `tests/test_ai_routes.py`; `make check` clean.

---

### Phase 29 — Wake-Word Voice Commands (on-robot) ✅

**Goal:** Hands-free AI control: the robot listens on its own USB microphone
for a configurable catch phrase ("hey nomon"), plays a chime, captures the
spoken command, transcribes it with the *shared* Vosk model, and dispatches it
to the same `AiCommandService` the app's chat bar uses — with a follow-up
window so the operator can keep talking without re-waking. Extends ADR-020's
"robot mic not involved" boundary for the operator command path only; still no
cognition in nomothetic (ADR-004), autonomon uninvolved, nomopractic unchanged.
See ADR-021.

**Cross-repo dependencies:** none (nomopractic's `enable_speaker` /
`disable_speaker` / `set_volume` / `set_mic_gain` IPC already existed).

#### 29.1 — Listener (`nomothetic.wake`)
- [x] `WakeWordListener` daemon thread: RMS-gated grammar decode →
      wake chime → utterance capture (Vosk endpointing + timeouts) → dispatch
      on the app loop → success/error chime → follow-up window (history capped
      at 20 turns, reset on silence).
- [x] `VoskSttEngine.create_recognizer()` — wake + command recognizers share
      the one lazily-loaded model (512 MB Pi Zero 2W).
- [x] Synthesized chimes (`media/audio/chimes/*.wav`, operator-replaceable);
      amp enable/volume/disable via HAT IPC around each chime; mic stream
      closed during chimes and AI execution (no self-hearing).
- [x] Graceful degradation: missing pyaudio/vosk/model/phrase → listener off
      with an actionable log; mic unplugged → backoff retry; capture-rate
      probing (16 k → 48 k → 44.1 k) + pure-Python downsample to 16 kHz.

#### 29.2 — Wiring, config & endpoints
- [x] Constructed in `create_app()` (side-effect free), auto-started in the
      lifespan when `NOMON_WAKE_PHRASE` is set; stopped on shutdown;
      `/api/audio/record/*` pause/resume the listener (ALSA exclusivity).
- [x] `GET/PUT /api/voice/wake` (`nomothetic.wake_routes`) — status + runtime
      enable/disable/phrase/variant updates (in-memory; env is boot config).
- [x] `NOMON_WAKE_*` env vars (`.env.device.example`), `[wakeword]` section in
      `config.toml` emitted conditionally by `scripts/start.sh`.

#### 29.3 — Deployment
- [x] `nomothetic-api.service`: `SupplementaryGroups=audio`; deploy.sh adds
      the service user to `audio` (/dev/snd for mic + DAC — a pre-existing gap
      for the audio endpoints too).
- [x] deploy.sh provisions the voice stack: ffmpeg + unzip join the apt
      package check, and the Vosk model `NOMON_STT_MODEL_PATH` names is
      installed when missing (stale `vosk-model-*` trees removed first);
      `fetch_stt_model.sh` derives the model name from the configured path.
- [x] `docs/pi_setup.md` §5.2 — prerequisites, enabling, and the
      out-of-vocabulary variant tuning loop for the wake phrase.

#### Phase 29 Exit Criteria
- [x] Tests: `tests/test_wake.py` (state machine, chimes, gating, pause/stop)
      + `tests/test_wake_routes.py` (endpoints, lifespan, record contention) +
      `create_recognizer` cases in `tests/test_stt.py`; `make check` clean.
- [ ] On-device: phrase → chime → spoken command → robot action → success
      chime; follow-up command without re-waking; `/api/audio/record` works
      while the listener is enabled. (Verify at next deploy.)

---

### Management Server

Developed in a separate repository.

**Expected interface:**
- MQTT broker (receives telemetry from fleet)
- Version manifest endpoint (serves release metadata for OTA)
- Object storage (S3-compatible) for release artifacts
- Admin dashboard for fleet monitoring

**AWS IoT path:** If AWS IoT is adopted, the management server uses
AWS IoT Core as the MQTT broker and AWS IoT Jobs for fleet update dispatch.
See ADR-007 and [docs/phase5_planning.md](phase5_planning.md).
