# nomon — Architecture

## System Overview

`nomon` runs on a small fleet of Raspberry Pi microcontrollers, each operating independently as a self-contained node. A mobile app and centralized management server interact with each Pi via its REST API.

```
┌─────────────────────────────────────────────────────────────────┐
│  Client Layer                                                   │
│                                                                 │
│   Mobile App          Mgmt Server             Admin (SSH)       │
│       │                   │                        │            │
│       │ HTTPS :8443        │ MQTT telemetry         │ Tailscale  │
└───────┼───────────────────┼────────────────────────┼────────────┘
        │                   │                        │
┌───────▼───────────────────▼────────────────────────▼────────────┐
│  Raspberry Pi Zero 2 W — Debian GNU/Linux 13 (trixie)           │
│                                                                 │
│   nomothetic.api (FastAPI/uvicorn)    StreamServer (Flask/MJPEG)     │
│         │                                │                      │
│   nomothetic.camera (picamera2) ──────────────┘                      │
│         │                                                       │
│   nomothetic.telemetry (paho-mqtt) ────────────► MQTT broker         │
│         │                                                       │
│   nomothetic.hat.HatClient                                           │
│         │  NDJSON over Unix socket (/run/nomopractic/nomopractic.sock)   │
│         ▼                                                       │
│   nomopractic.service (Rust daemon)                               │
│         │  rppal (pure-Rust I2C/GPIO)                           │
│         ▼                                                       │
│   SunFounder Robot HAT V4  ──  I2C bus 1, address 0x14         │
│         │                                                       │
│   OV5647 camera ── I2C bus 10/11, address 0x36 (muxed)         │
└─────────────────────────────────────────────────────────────────┘
```

---

## Deployment Modes

nomothetic runs in one of two mutually exclusive modes, selected by the
`NOMON_API_MODE` environment variable (see ADR-011):

| Mode | `NOMON_API_MODE` | Deployment | Routes |
|------|-----------------|-----------|--------|
| **Device** | `device` (default) | Each Raspberry Pi | Hardware, camera, audio, calibration, routine |
| **Central** | `central` | Dedicated server | Auth, user management, fleet data |

```
                    ┌──────────────────────────────────┐
                    │       nomotactic (Expo app)       │
                    │    Android / iOS / Web            │
                    └─────────┬────────────┬────────────┘
                              │            │
                    HTTPS :443│            │HTTPS :8443
                              ▼            ▼
                    ┌─────────────┐  ┌─────────────────┐
                    │ nomothetic  │  │   nomothetic     │
                    │ central mode│  │   device mode    │
                    │             │  │   (on Pi)        │
                    │ Auth        │  │   Camera, HAT,   │
                    │ Fleet       │  │   Audio, Stream, │
                    │ User mgmt  │  │   Calibration,   │
                    │             │  │   Routines       │
                    └──────┬──────┘  └─────────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  ArcadeDB   │
                    │  (central)  │
                    └─────────────┘
```

**Wi-Fi Soft AP note:** When the device is not connected to a known Wi-Fi
network, `nomopractic/scripts/ap-mode.sh` activates a WPA2 hotspot
(`nomon-<last4-of-MAC>`).  The passphrase is the shared pairing secret
generated on first boot.  One service starts on the AP interface:

- **`nomothetic-ap.service`** — plain HTTP on `192.168.4.1:8080`; full
  device API bound exclusively to the AP gateway address so it is unreachable
  from other interfaces (see ADR-016).  Cleartext is acceptable: the AP is a
  closed WPA2 hotspot on an isolated `192.168.4.0/24` subnet.

**End-to-end provisioning sequence:**

1. Device boots off-network → `nomon-softap.service` starts AP at `192.168.4.1`;
   `nomothetic-ap.service` starts at `192.168.4.1:8080` (plain HTTP)
2. User connects to `nomon-<last4>` AP using the 6-digit pairing secret (WPA2 PSK)
3. App probes `GET http://192.168.4.1:8080/api/device/auth/status`; detects AP
4. App submits pairing request to `POST /api/device/auth/pair/ap` → receives
   device JWT (JWT signing secret persisted in `/var/lib/nomon/device_jwt_secret`;
   survives AP → Wi-Fi mode switch — no re-pairing required)
5. App reveals Wi-Fi provisioning form; user enters home SSID + WPA2 password
6. `POST /api/device/network/configure` → nomothetic invokes `nmcli --ask` in a
   non-blocking background task, writing the WPA2 password to stdin so it never
   appears in the process argument list; NM stores a persistent connection profile
7. `nomon-softap-watchdog.service` polls connectivity every 30 s; when
   `nmcli general connectivity` reaches `full`, calls `ap-mode.sh down`
8. On future boots, device connects to home network directly; AP only activates
   if home network is unreachable

`POST /api/device/wifi/ap` provides manual control over the AP
(`{ "subcommand": "up" | "down" }`). It invokes `ap-mode.sh <subcommand>` via
`subprocess.run` in a thread-pool executor. The script path is configured via
`NOMON_AP_MODE_SCRIPT` (default: `/opt/nomon/scripts/ap-mode.sh`). The
subcommand is validated against the `{"up", "down"}` allowlist before being
passed to the script — it is never taken verbatim from user input.

- **Device mode** is the existing configuration — all current endpoints work
  unchanged. Hardware-specific libraries are conditionally imported.
- **Central mode** never imports hardware libraries (picamera2, pyaudio,
  rppal socket). It requires `[auth]` and `[central]` optional dependencies.
- The health endpoint (`GET /`) is available in both modes.
- Route registration is conditional — each mode's `create_app()` only mounts
  its own route group. No "dead" endpoints returning errors on the wrong server.

### Central Fleet Registration

After pairing via Soft AP, an authenticated central user can register the
device to their fleet account using a two-step flow:

**Step 1 — Obtain device identity and proof**

The client calls `GET /api/device/auth/identity` with a valid device JWT.
The device returns:
- `vin` — the hardware vehicle identification number
- `model` — the device model string
- `hostname` — the device mDNS hostname (e.g. `nomon-abcd.local`)
- `registration_proof` — a short-lived JWT (5-minute TTL) signed by the
  device secret, with claims `iss=nomon-device`, `sub=<vin>`,
  `aud=nomon-fleet`, and a unique `jti`

**Step 2 — Register with the central API**

The client calls the central `POST /api/fleet/devices` with a valid central
JWT and the body `{ vin, model, registration_proof }`. The central API
validates the proof structurally: expiry (`exp`), VIN binding
(`sub == submitted vin`), and audience (`aud == "nomon-fleet"`).

**Note on cryptographic verification:** The central server validates the
proof structurally but cannot verify the device's HMAC signature — the
device and central services use separate JWT secrets. Full cryptographic
ownership verification is planned for a future phase using asymmetric device
certificates. See ADR-017 for the design rationale and trade-offs.

---

## Module Responsibilities

### `nomothetic.camera` — `Camera`

The lowest-level hardware abstraction. Wraps `picamera2` directly.

**Responsibilities:**
- Initialize and configure the OV5647 sensor
- Still image capture → JPEG files on disk
- Video recording → H264/MJPEG files on disk
- Provide a JPEG frame generator for streaming consumers
- Enforce filename safety (no path traversal)

**Key design decisions:**
- Conditional `picamera2` import — module is importable on non-Pi systems
- `directory` parameter controls where all files are written; never allows escape
- Single encoder instance; switching encoder requires reinitialization
- `get_jpeg_frame_generator()` yields raw JPEG bytes — both `StreamServer` and future direct callers use this

**Does NOT:**
- Serve HTTP
- Do network I/O
- Have awareness of the REST API

---

### `nomothetic.streaming` — `StreamServer`

A lightweight local LAN viewer. Not used by the mobile app.

**Responsibilities:**
- Create a `Camera` instance internally
- Serve an HTML viewer page at `/`
- Serve an MJPEG stream at `/stream` (multipart/x-mixed-replace)
- Run in foreground (`start()`) or background thread (`start_background()`)

**Key design decisions:**
- Flask chosen for minimal overhead — two endpoints only (see ADR-003)
- HTTP (not HTTPS) — LAN-only, not exposed to mobile clients
- Thread-safe frame sharing via `_frame_lock`
- Default binding: `localhost` — must be explicitly changed for LAN access

**Port:** 8000 (default, configurable)

---

### `nomothetic.api` — `APIServer` / `create_app()`

The primary remote control interface. Mobile app and management server talk to this.

**Responsibilities:**
- Expose camera operations as a JSON REST API
- Terminate HTTPS/TLS connections using self-signed certs
- Auto-generate self-signed certs on first run (stored in `.certs/`)
- Run in foreground (`run()`) or background thread (`start_background()`)
- Validate all incoming request data via Pydantic models

**Key design decisions:**
- FastAPI chosen for automatic OpenAPI docs and Pydantic integration (see ADR-002)
- Self-signed certs chosen for zero-configuration private network deployment (see ADR-001)
- CORS `allow_origins=["*"]` in development — restrict for production
- Global `_camera` instance managed by FastAPI lifespan context manager
- All responses include a UTC `timestamp` ISO 8601 field

**Port:** 8443 (default, configurable)

**Endpoints:**

| Method | Path | Tag | Description |
|--------|------|-----|-------------|
| `GET` | `/` | Health | Health check |
| `GET` | `/api/camera/status` | Camera | Camera state (resolution, fps, encoder, recording) |
| `POST` | `/api/camera/capture` | Camera | Still image capture (writes a file; returns metadata) |
| `GET` | `/api/camera/frame` | Camera | Single raw JPEG frame (`image/jpeg` bytes) — raw input for autonomon vision |
| `POST` | `/api/camera/record/start` | Camera | Start video recording |
| `POST` | `/api/camera/record/stop` | Camera | Stop video recording |
| `POST` | `/api/camera/pan` | Vehicle | Set camera pan servo angle |
| `POST` | `/api/camera/tilt` | Vehicle | Set camera tilt servo angle |
| `GET` | `/api/hat/battery` | HAT | Read HAT battery voltage |
| `POST` | `/api/hat/servo` | HAT | Set servo channel angle |
| `POST` | `/api/hat/reset` | HAT | Assert MCU reset |
| `GET` | `/api/hat/servo/status` | HAT | Active servo TTL leases |
| `GET` | `/api/hat/mcu/status` | HAT | MCU reset counter |
| `POST` | `/api/hat/motor` | HAT | Set DC motor channel speed |
| `POST` | `/api/hat/motor/stop` | HAT | Stop all DC motors |
| `GET` | `/api/hat/motor/status` | HAT | Active motor TTL leases |
| `POST` | `/api/hat/speaker` | HAT | Enable/disable speaker amplifier (BCM 20) |
| `POST` | `/api/drive` | Vehicle | Drive all motors at signed speed |
| `POST` | `/api/steer` | Vehicle | Set steering servo angle |
| `GET` | `/api/sensor/grayscale` | Vehicle | Read grayscale ADC channels |
| `GET` | `/api/sensor/ultrasonic` | Sensor | Trigger ultrasonic and return distance |
| `POST` | `/api/stream/start` | Stream | Start MJPEG stream server |
| `POST` | `/api/stream/stop` | Stream | Stop MJPEG stream server |
| `GET` | `/api/stream/status` | Stream | Current stream server state |
| `POST` | `/api/audio/record/start` | Audio | Start USB mic recording |
| `POST` | `/api/audio/record/stop` | Audio | Stop USB mic recording |
| `POST` | `/api/audio/play` | Audio | Play WAV over the USB codec speaker output |
| `POST` | `/api/audio/play/stop` | Audio | Stop audio playback |
| `GET` | `/api/audio/files` | Audio | List available WAV files |
| `GET` | `/api/audio/status` | Audio | Current recorder/player state |
| `GET` | `/api/audio/volume` | Audio | Read current output volume (0–100) |
| `POST` | `/api/audio/volume` | Audio | Set output volume (ALSA mixer) |
| `GET` | `/api/audio/mic-gain` | Audio | Read current mic capture gain (0–100) |
| `POST` | `/api/audio/mic-gain` | Audio | Set mic capture gain (USB mic PCM2902) |
| `GET` | `/api/voice/wake` | Voice | Wake-word listener status (ADR-021) |
| `PUT` | `/api/voice/wake` | Voice | Enable/disable wake listener, update phrase |
| `GET` | `/api/sensor/grayscale/normalized` | Sensor | Normalised grayscale sensor readings (0.0–1.0) |
| `GET` | `/api/calibration` | Calibration | Full calibration snapshot |
| `PUT` | `/api/calibration/motor/{channel}` | Calibration | Set motor calibration (speed_scale, deadband, reversed) |
| `PUT` | `/api/calibration/servo/{servo_name}` | Calibration | Set servo trim offset (µs) |
| `POST` | `/api/calibration/grayscale/{channel}/capture` | Calibration | Capture live ADC reading as white/black reference |
| `POST` | `/api/calibration/save` | Calibration | Persist calibration to disk |
| `POST` | `/api/calibration/reset` | Calibration | Revert in-memory calibration to defaults |
| `POST` | `/api/routine/start` | Routine | Start a named firmware HAT routine (nomopractic, ADR-009) |
| `POST` | `/api/routine/stop` | Routine | Stop the active firmware HAT routine; returns run statistics |
| `GET` | `/api/routine/status` | Routine | Query active firmware HAT routine state |
| `GET` | `/api/routines/available` | Autonomy | List routines this device can launch, read from the catalogue file autonomon publishes (`NOMON_ROUTINE_CATALOG_PATH`); empty when none has been published (autonomon ADR-005) |
| `POST` | `/api/routines/start` | Autonomy | Launch an autonomy routine from a JSON payload (`routine`, `params`, optional `heartbeat_timeout_s`/`max_duration_s`); supervised as a subprocess under a renewable heartbeat lease |
| `POST` | `/api/routines/heartbeat` | Autonomy | Renew a running routine's lease (`routine`); the app calls this on an interval to keep the routine alive while contact holds |
| `POST` | `/api/routines/stop` | Autonomy | Stop one running autonomy routine (`routine`) |
| `POST` | `/api/routines/stop-all` | Autonomy | Stop every running autonomy routine |
| `GET` | `/api/routines` | Autonomy | Status summary of every autonomy routine that has reported |
| `GET` | `/api/routines/{routine}/logs` | Autonomy | Current status + recent lifecycle events for a routine |
| `POST` | `/api/routines/{routine}/events` | Autonomy | Append a reported lifecycle event (plugin → gateway, push model) |
| `POST` | `/api/device/wifi/ap` | Device | Toggle Soft AP up or down (`up`/`down`) |
| `GET` | `/api/device/auth/identity` | Device | Return device hardware VIN, model, hostname, and a short-lived registration proof JWT for fleet registration |
| `GET` | `/docs` | — | Interactive Swagger UI |
| `GET` | `/redoc` | — | ReDoc API docs |

**HTTP Status Codes:**

| Code | Meaning |
|------|---------|
| `200` | Success |
| `400` | Bad request (invalid filename, bad parameters) |
| `409` | Conflict (recording already in/not in progress; routine ALREADY_RUNNING or not running) |
| `422` | Unprocessable Entity (Pydantic validation failure — invalid param type or range) |
| `500` | Server/hardware error |
| `503` | HAT daemon unavailable (nomopractic not running) |

---

### `nomothetic.telemetry` — `TelemetryPublisher`
A background telemetry publisher. Sends structured JSON to an MQTT broker.

**Responsibilities:**
- Discover device identity (env var → Pi serial → hostname)
- Build a JSON telemetry payload (device ID, timestamp, nomothetic version, camera status)
- Publish periodically over MQTT in a daemon background thread
- Handle broker unavailability with exponential back-off reconnect
- Expose a one-shot `publish_now()` for scripted or ad-hoc use

**Key design decisions:**
- Conditional `paho-mqtt` import — module is importable without paho-mqtt installed
- Fully standalone — no coupling to `APIServer` or `StreamServer` lifecycle
- `threading.Event` shutdown signal for clean daemon thread exit
- Back-off: 1 s → 2 s → 4 s → … capped at 60 s; resets on successful connect
- Camera is optional — payload `"camera"` field is `null` if no `Camera` provided
- All config via env vars (`NOMON_MQTT_*`) or constructor arguments

**Does NOT:**
- Receive MQTT messages (subscribe)
- Expose HTTP endpoints
- Block the REST API

**Port:** N/A — uses MQTT (default TCP 1883)

---

### `nomothetic.hat` — `HatClient`

The IPC client for the `nomopractic` Rust daemon. See
[docs/hat_python_client.md](hat_python_client.md) for the full module design.

**Responsibilities:**
- Open and maintain a connection to `/run/nomopractic/nomopractic.sock`
- Serialise requests and deserialise responses (NDJSON)
- Expose typed Python methods (`get_battery_voltage`, `set_servo_angle`, etc.)
- Raise `HatConnectionError` if the daemon is not running
- Apply per-request timeout

**Key design decisions:**
- Contains *no hardware register logic* — all hardware knowledge is in the Rust daemon
- `asyncio.to_thread` wraps blocking socket calls for FastAPI route handlers
- Persistent connection with automatic reconnect on broken pipe
- Follows the same conditional-import pattern as other `nomothetic` modules

**Does NOT:**
- Know about I2C addresses, PWM registers, ADC scaling
- Run its own thread — called synchronously from route handlers (wrapped in `to_thread`)

---

### `nomothetic.audio` — `AudioRecorder` / `AudioPlayer`

Handles USB codec recording and playback. Speaker amplifier
enable/disable is delegated to `HatClient` (nomopractic BCM 20 GPIO).

**Hardware:**
- **Microphone**: USB PnP Sound Device (Texas Instruments PCM2902), ALSA card 1
- **Speaker output**: the same USB PCM2902 codec (ALSA card 1) — the only other
  sound card on the robot is the playback-only HDMI output (`vc4hdmi`, ALSA
  card 0); there is no HifiBerry DAC despite earlier docs
- **Speaker amplifier enable**: BCM 20 (`spk_en` on Robot HAT V4), controlled
  via `nomopractic` IPC (`enable_speaker` / `disable_speaker`)

**Responsibilities:**
- `AudioRecorder.start()`: resolves the mic by PortAudio device *name*
  (`NOMON_AUDIO_INPUT_NAME`, default match `"usb"`; `NOMON_AUDIO_INPUT_INDEX`
  as explicit override), records in a background thread, closes and writes WAV
  on `stop()`
- `AudioPlayer.play()`: opens PyAudio output stream on the name-resolved USB
  codec output (`NOMON_AUDIO_OUTPUT_NAME`/`NOMON_AUDIO_OUTPUT_INDEX`), plays
  WAV chunks in background thread
- `list_audio_files()`: lists `*.wav` files in the configured audio directory
- Graceful degradation: `RuntimeError` raised when `pyaudio` is not installed
- Devices are resolved by name, never a blind numeric default: PortAudio
  indexes are not ALSA card numbers, and opening the wrong capture device
  segfaulted libportaudio on the Pi (killing the whole API process)

**Key design decisions:**
- Camera stays in `nomothetic` (complex libcamera Python interface, no HAT GPIO)
- USB microphone stays in `nomothetic` (USB audio, no HAT GPIO interface needed)
- Speaker GPIO enable stays in `nomopractic` (HAT pin, same as all other GPIO)
- Audio playback stays in `nomothetic` (PyAudio/ALSA, Python-native)
- Background threading (not asyncio) for pyaudio compatibility
- `threading.Event` stop signal; thread joins with 3 s timeout

**Optional dependency:** `pyaudio>=0.2.14` in `[audio]` extra group

---

### `nomothetic.wake` — `WakeWordListener` (ADR-021)

Hands-free AI voice commands: a daemon thread streams the USB microphone into
a grammar-constrained Vosk recognizer (catch phrase from `NOMON_WAKE_PHRASE`),
chimes on wake, captures the spoken command with the full-vocabulary
recognizer, and dispatches it to the same `AiCommandService` the app's chat
bar uses — then keeps a follow-up window open so the operator can continue the
conversation without re-waking.

**Responsibilities:**
- Wake detection: RMS-gated streaming decode against `[phrase, variants, "[unk]"]`
- Utterance capture: Vosk endpointing + no-speech timeout + hard utterance cap
- Dispatch: `asyncio.run_coroutine_threadsafe` onto the app loop →
  `AiCommandService.run_command` (history capped at 20 turns)
- Feedback: synthesized wake/processing/success/error chimes
  (`media/audio/chimes/`), amp enable/volume/disable via `HatClient` around
  each clip; the processing blip plays once the utterance is captured, while
  the transcript is finalised and the AI command runs
- Spoken echo: with a `TtsEngine` wired, the heard transcript is spoken back
  on a side thread *concurrently with the AI dispatch* (`_speak_async` →
  synthesize → shared `_play_wave`), covering the silent gap without adding to
  it; missing espeak-ng/ffmpeg falls back to the processing chime alone
- Contention: mic stream closed during chimes, the spoken echo, and AI
  execution; `pause()`/`resume()` hooks called by the `/api/audio/record`
  endpoints
- Runtime control: `GET/PUT /api/voice/wake` (`nomothetic.wake_routes`)

**Key design decisions:**
- Shares the one lazily-loaded Vosk model via `VoskSttEngine.create_recognizer()`
  (512 MB Pi Zero 2W — never two model copies)
- Operator command path only — no cognition (ADR-004), autonomon uninvolved
- Construction is side-effect free; missing pyaudio/vosk/model/phrase leaves
  the listener off without affecting the API

**Optional dependencies:** `[audio]` (pyaudio) + `[stt]` (vosk) extras

---

### `nomothetic.tts` — `EspeakTtsEngine` (ADR-021)

Offline speech synthesis for the wake-word listener's spoken transcript echo.
Pluggable behind the `TtsEngine` protocol (mirrors `nomothetic.stt`), swapped
in `create_app()` without touching the listener.

**Responsibilities:**
- `synthesize(text) -> bytes`: shell out to `espeak-ng --stdout` (text passed
  as one argv element — no shell, no injection), then normalise via an ffmpeg
  subprocess to 44.1 kHz mono 16-bit WAV so it plays through the listener's
  chime output path with no sample-rate mismatch on the USB codec
- Config: `NOMON_TTS_VOICE` (default `en`), `NOMON_TTS_RATE_WPM` (default 160)

**Key design decisions:**
- espeak-ng is a formant synthesiser — faster than real time on the Pi Zero
  2W, so a short phrase genuinely overlaps the network-bound AI dispatch
- No model to load (unlike Vosk); each call is two short subprocesses
- Missing `espeak-ng`/`ffmpeg` → `TtsUnavailableError`, which the listener
  treats as "no spoken echo" (the processing chime still plays), never an error

**System prerequisites:** `espeak-ng` + `ffmpeg` (apt; installed by deploy.sh)

---

## Data Flow — Still Capture

```
Mobile App
  POST /api/camera/capture {"filename": "photo.jpg"}
        │
  APIServer (FastAPI route)
        │ validates filename
        │ calls Camera.capture_image("photo.jpg")
        │
  Camera
        │ starts picamera2 still config
        │ captures frame to disk at <directory>/photo.jpg
        │ returns
        │
  APIServer
        └─► 200 {"success": true, "filename": "photo.jpg", "timestamp": "..."}
```

---

## Data Flow — MJPEG Stream

```
Browser / LAN Client
  GET /stream (HTTP)
        │
  StreamServer (Flask)
        │ opens multipart/x-mixed-replace response
        │
  Camera.get_jpeg_frame_generator()
        │ yields JPEG bytes from picamera2
        │
  StreamServer
        └─► streams boundary-wrapped JPEG frames continuously
```

---

## Data Flow — HAT Battery Voltage

```
Mobile App
  GET /api/hat/battery
        │
  APIServer (FastAPI route)
        │ asyncio.to_thread(hat_client.get_battery_voltage)
        │
  HatClient (nomothetic.hat)
        │ {"id":"1","method":"get_battery_voltage","params":{}}\n
        │  →  Unix socket  →  nomopractic.service (Rust)
        │       I2C read: bus 1, addr 0x14, ADC channel A4
        │  ←  {"id":"1","ok":true,"result":{"voltage_v":7.42}}\n
        │
  APIServer
        └─► 200 {"voltage_v": 7.42, "timestamp": "..."}
```

---

## Data Flow — HAT Servo Angle

```
Mobile App
  POST /api/hat/servo {"channel": 0, "angle_deg": 90.0}
        │
  APIServer (FastAPI route)
        │ asyncio.to_thread(hat_client.set_servo_angle, 0, 90.0)
        │
  HatClient (nomothetic.hat)
        │ {"id":"2","method":"set_servo_angle","params":{"channel":0,"angle_deg":90.0,"ttl_ms":500}}\n
        │  →  Unix socket  →  nomopractic.service (Rust)
        │       I2C PWM write: pulse_us=1611 on channel 0
        │  ←  {"id":"2","ok":true,"result":{"channel":0,"angle_deg":90.0,"pulse_us":1611}}\n
        │
  APIServer
        └─► 200 {"channel": 0, "angle_deg": 90.0, "pulse_us": 1611, "timestamp": "..."}
```

---



| Concern | Approach |
|---------|----------|
| Transport encryption | TLS 1.2+ via uvicorn; self-signed cert auto-generated |
| Authentication (device mode) | None — relies on Tailscale VPN for network-layer access control |
| Authentication (central mode) | Self-hosted JWT (HS256 access + refresh tokens); see ADR-010 |
| Path traversal | Filename-only validation in `Camera`; rejects `/`, `\`, `..`, `.` prefix, absolute paths |
| CORS (device mode) | `allow_origins=["*"]` in dev; tighten for production |
| CORS (central mode) | Explicit origins from `NOMON_CORS_ORIGINS` env var; no wildcard on auth routes |
| Secrets | `.env.device` and `.env.central` are gitignored; `.certs/` is gitignored; `NOMON_JWT_SECRET` required in central mode |

---

## Dependency Map

```
nomothetic.hat
  ├── socket (stdlib)
  ├── json (stdlib)
  └── (no hardware deps — all hardware is in the Rust daemon)

nomothetic.api
  ├── nomothetic.camera
  ├── nomothetic.hat           (HatClient — IPC to nomopractic daemon)
  ├── nomothetic.auth          (central mode — JWT auth)
  ├── nomothetic.mode          (device/central mode selection)
  ├── nomothetic.rate_limit    (auth endpoint rate limiting)
  ├── nomothetic.fleet_routes  (central mode — fleet CRUD)
  ├── nomothetic.db            (central mode — ArcadeDB client, optional)
  ├── nomothetic.user_store    (user persistence — InMemory or Gremlin)
  ├── nomothetic.fleet_store   (fleet persistence — InMemory or Gremlin)
  ├── fastapi
  ├── uvicorn
  ├── pydantic
  ├── cryptography
  └── python-dotenv

nomothetic.db
  └── httpx  (optional — conditional import)

nomothetic.user_store
  ├── nomothetic.auth   (UserRecord dataclass)
  └── nomothetic.db     (DatabaseClient, TYPE_CHECKING only)

nomothetic.fleet_store
  ├── pydantic          (DeviceItem model)
  └── nomothetic.db     (DatabaseClient, TYPE_CHECKING only)

nomothetic.auth
  ├── authlib           (optional — [auth] extra)
  ├── bcrypt            (optional — [auth] extra)
  └── nomothetic.user_store (UserStore protocol)

nomothetic.streaming
  ├── nomothetic.camera
  └── flask

nomothetic.camera
  ├── picamera2  (Pi only — install [pi] extra; conditional import)
  └── (no other runtime deps)

nomothetic.telemetry
  ├── nomothetic (for __version__)
  ├── paho-mqtt  (optional — conditional import)
  └── (standard library: threading, json, socket, os)
```

---

## Phase 5 — HAT Module Driver (Rust, Separate Repo)

The `nomopractic` Rust daemon (see ADR-006) runs as `nomopractic.service` and
communicates with `nomothetic.api` via a Unix domain socket at
`/run/nomopractic/nomopractic.sock`. Python was evaluated and rejected for HAT
drivers due to GIL-induced latency in timing-critical GPIO/I2C operations.

**Hardware confirmed:** SunFounder Robot HAT V4 on I2C bus 1 at address `0x14`.
See [docs/pi_hardware.md](pi_hardware.md) for discovery details.

**IPC:** `nomothetic.hat.HatClient` (Python) connects to the socket and exchanges
NDJSON messages with the Rust daemon. The full schema is defined in
[docs/hat_ipc_schema.md](hat_ipc_schema.md).

`nomothetic.api` HAT endpoints (`/api/hat/...`) proxy requests via `HatClient`.
If the daemon is not running, HAT endpoints return `503 Service Unavailable`.

**First milestone deliverables:** battery voltage reading + servo angle control.
See [docs/hat_python_client.md](hat_python_client.md) for the Python client design.

---

## Repository Strategy

All Python modules remain in this single repository. None of them have external
consumers or independent release cadences, so there is no benefit to splitting
them. Updates are applied atomically: a single `git pull` moves all modules to
the same commit simultaneously.

The Rust HAT daemon (`nomopractic`) lives in a separate repository because it
produces a different build artifact (compiled binary), uses a different update
mechanism (artifact download, not git), runs as a separate systemd service,
and has an independent release cadence. See ADR-006 for the full rationale.

```
nomothetic/              ← Python monorepo (this repo)
  nomothetic.camera
  nomothetic.streaming
  nomothetic.api
  nomothetic.telemetry
  nomothetic.hat          ← IPC client for nomopractic (Phase 5)
  nomothetic.audio        ← USB mic recording + DAC playback (Phase 8)
  nomothetic.wake         ← Wake-word AI voice commands (Phase 29, ADR-021)
  nomothetic.auth         ← JWT auth service (Phase 13, central mode)
  nomothetic.mode         ← Device/central mode selection (Phase 13)
  nomothetic.rate_limit   ← Sliding-window rate limiter (Phase 13)
  nomothetic.fleet_routes ← Fleet data REST endpoints (Phase 13)
  nomothetic.db           ← ArcadeDB HTTP/Gremlin client (Phase 14)
  nomothetic.user_store   ← User persistence: InMemory + Gremlin backends (Phase 14)
  nomothetic.fleet_store  ← Fleet persistence: InMemory + Gremlin backends (Phase 14)
  nomothetic.telemetry_store    ← Telemetry history persistence: InMemory + SQL (Phase 25)
  nomothetic.telemetry_consumer ← Central MQTT subscriber → telemetry_store (Phase 25)

nomopractic/          ← Rust repo (Phase 5, separate)
  Cargo.toml
  src/main.rs
  systemd/nomopractic.service
```
