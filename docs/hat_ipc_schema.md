# nomopractic IPC Schema

## Overview

`nomothetic.api` (Python) communicates with the `nomopractic` daemon (Rust) through a
**Unix domain socket** using **newline-delimited JSON (NDJSON)** framing.

This document is the interface contract between the two processes. Both sides
must implement this schema exactly; changes require coordinated releases.

---

## Transport

| Property | Value |
|----------|-------|
| Mechanism | Unix domain socket (SOCK_STREAM) |
| Default path | `/run/nomopractic/nomopractic.sock` |
| Config override | `NOMON_HAT_SOCKET_PATH` env var |
| Direction | Client-initiated (nomothetic.api connects; nomopractic listens) |
| Connections | Short-lived per-request or persistent; daemon accepts multiple |

The Unix domain socket was chosen over localhost HTTP because:
- No port allocation or conflicts
- Kernel-enforced process isolation (file permissions on socket path)
- Lower overhead than TCP loopback for frequent servo/ADC calls
- Simpler to secure with filesystem ACLs (`chmod 660`, `chown root:nomon`)

---

## Framing

Each message (request or response) is a single JSON object terminated by a
`\n` (newline, U+000A). Receivers buffer bytes until `\n`, then parse the
complete JSON object.

NDJSON was chosen over length-prefixed framing because:
- Text-based — can be debugged interactively with `socat` or `nc`
- No 4-byte length-field parsing required; standard JSON libraries handle it
- Messages are short (< 1 kB); length-prefix overhead savings are negligible
- Familiar convention for streaming logs and inter-process JSON protocols

### Rules

- Each message MUST end with exactly one `\n`
- A message MUST NOT contain an embedded `\n` inside JSON string values (use `\n` JSON escape)
- Maximum message length: 4096 bytes (daemon enforces; client should not exceed)
- Encoding: UTF-8

---

## Request Envelope

```json
{"id": "req-001", "method": "get_battery_voltage", "params": {}}\n
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Caller-assigned request identifier; echoed in response |
| `method` | string | yes | Method name (see [Methods](#methods) below) |
| `params` | object | yes | Method parameters (empty object `{}` if none) |

The `id` field is opaque to the daemon. The Python client should use a
short unique identifier per request (e.g., sequential integer as string, or
short UUID prefix).

---

## Response Envelope

**Success:**
```json
{"id": "req-001", "ok": true, "result": {"voltage_v": 7.42}}\n
```

**Error:**
```json
{"id": "req-001", "ok": false, "error": {"code": "HARDWARE_ERROR", "message": "I2C read failed: EREMOTEIO"}}\n
```

| Field | Type | Always present | Description |
|-------|------|----------------|-------------|
| `id` | string | yes | Echoed from request |
| `ok` | bool | yes | `true` on success, `false` on error |
| `result` | object | when `ok=true` | Method-specific result payload |
| `error` | object | when `ok=false` | Error details |
| `error.code` | string | when `ok=false` | Machine-readable error code (see below) |
| `error.message` | string | when `ok=false` | Human-readable description |

### Error Codes

| Code | Meaning |
|------|---------|
| `UNKNOWN_METHOD` | The requested method name is not recognised |
| `INVALID_PARAMS` | One or more required params are missing or out of range |
| `HARDWARE_ERROR` | I2C/SPI/GPIO operation failed at the OS level |
| `NOT_READY` | Daemon is initialising; retry after a short delay |
| `SERVO_LEASE_EXPIRED` | Servo lease TTL elapsed — servo channel idled (pulse_us=0) until a new command is issued |
| `ALREADY_RUNNING` | A routine is already active; call `stop_routine` before starting a new one |
| `INTERNAL_ERROR` | Unexpected daemon error (bug) |

---

## Methods

### `health`

Returns daemon liveness and hardware connection status.

**Request:**
```json
{"id": "1", "method": "health", "params": {}}\n
```

**Response (`result`):**
```json
{
  "schema_version": "1.0.0",
  "status": "ok",
  "version": "0.1.0",
  "hat_address": "0x14",
  "i2c_bus": 1,
  "uptime_s": 3600
}
```

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | string | IPC schema semver (see [Versioning](#versioning)); client should verify on connect |
| `status` | `"ok"` \| `"degraded"` | `"ok"` if I2C link is up; `"degraded"` otherwise |
| `version` | string | nomopractic semver |
| `hat_address` | string | I2C address in use (hex string, e.g. `"0x14"`) |
| `i2c_bus` | integer | Linux I2C bus number (default `1`) |
| `uptime_s` | integer | Seconds since daemon start |

---

### `get_battery_voltage`

Reads the battery voltage via ADC channel A4 on the Robot HAT V4.

**Hardware detail:** ADC command scheme sends `(7 - channel) | 0x10` as one
byte, then reads back 2 bytes. Raw ADC value is scaled: `battery_v = raw_v × 3`.

**Request:**
```json
{"id": "2", "method": "get_battery_voltage", "params": {}}\n
```

**Response (`result`):**
```json
{
  "voltage_v": 7.42,
  "raw_adc": 24700
}
```

| Field | Type | Description |
|-------|------|-------------|
| `voltage_v` | float | Battery voltage in volts (scaled) |
| `raw_adc` | integer | Raw 16-bit ADC reading before scaling |

---

### `read_adc`

Read a raw 16-bit value from one of the eight ADC channels on the Robot HAT V4
(ADS7830-compatible command protocol via I2C).

**Hardware detail:** Command byte is `(7 - channel) | 0x10`; two bytes are
read back and combined as a big-endian 16-bit value.

**Request:**
```json
{"id": "a1", "method": "read_adc", "params": {"channel": 3}}\n
```

| Param | Type | Required | Constraints |
|-------|------|----------|-------------|
| `channel` | integer | yes | 0–7 |

**Response (`result`):**
```json
{"channel": 3, "raw_value": 14823}
```

| Field | Type | Description |
|-------|------|-------------|
| `channel` | integer | ADC channel (echoed) |
| `raw_value` | integer | Raw 16-bit ADC reading |

**Errors:**

| Code | Condition |
|------|-----------|
| `INVALID_PARAMS` | `channel` absent or outside 0–7 |
| `HARDWARE_ERROR` | I2C read failure |

---

### `set_servo_pulse_us`

Sets a PWM channel to a specific pulse width in microseconds.

**Hardware detail:** Robot HAT V4 PWM controller (I2C 0x14):
- `REG_CHN=0x20`, `REG_PSC=0x40`, `REG_ARR=0x44`
- Clock: 72 MHz; servo period: PERIOD=4095; pulse width range: 500–2500 µs

**Request:**
```json
{"id": "3", "method": "set_servo_pulse_us", "params": {"channel": 0, "pulse_us": 1500, "ttl_ms": 500}}\n
```

| Param | Type | Required | Range | Description |
|-------|------|----------|-------|-------------|
| `channel` | integer | yes | 0–11 | PWM channel number |
| `pulse_us` | integer | yes | 500–2500 | Pulse width in microseconds |
| `ttl_ms` | integer | no | 100–5000 | Lease TTL (ms); servo idles if not refreshed. Default: 500 |

**Response (`result`):**
```json
{"channel": 0, "pulse_us": 1500}
```

---

### `set_servo_angle`

Convenience wrapper: converts an angle in degrees to a pulse width and calls
the PWM controller.

Mapping: `pulse_us = 500 + (angle / 180.0) × 2000`
(i.e., 0° → 500 µs, 90° → 1611 µs, 180° → 2500 µs)

**Request:**
```json
{"id": "4", "method": "set_servo_angle", "params": {"channel": 0, "angle_deg": 90.0, "ttl_ms": 500}}\n
```

| Param | Type | Required | Range | Description |
|-------|------|----------|-------|-------------|
| `channel` | integer | yes | 0–11 | PWM channel number |
| `angle_deg` | float | yes | 0.0–180.0 | Target angle in degrees |
| `ttl_ms` | integer | no | 100–5000 | Lease TTL (ms). Default: 500 |

**Response (`result`):**
```json
{"channel": 0, "angle_deg": 90.0, "pulse_us": 1611}
```

---

### `read_gpio`

Read the current logical level of a named GPIO pin.

**Available pins:** `D2`, `D3`, `D4`, `D5`, `MCURST`, `SW`, `LED`, `SPEAKER_EN`.
All pins are readable; `SW` (BCM 19) is the user push-button (input-only).

**Request:**
```json
{"id": "g1", "method": "read_gpio", "params": {"pin": "SW"}}\n
```

| Param | Type | Required | Constraints |
|-------|------|----------|-------------|
| `pin` | string | yes | One of the pin names listed above |

**Response (`result`):**
```json
{"pin": "SW", "high": false}
```

| Field | Type | Description |
|-------|------|-------------|
| `pin` | string | Pin name (echoed) |
| `high` | boolean | `true` if the pin is currently driven high |

**Errors:**

| Code | Condition |
|------|-----------|
| `INVALID_PARAMS` | `pin` absent or not a recognised pin name |
| `HARDWARE_ERROR` | GPIO read failure |

---

### `write_gpio`

Drive a named GPIO output pin high or low.

**Writable pins:** `D2`, `D4`, `D5`, `MCURST`, `LED`, `SPEAKER_EN`.
`D3` and `SW` are input-only and will return `INVALID_PARAMS`.

**Request:**
```json
{"id": "g2", "method": "write_gpio", "params": {"pin": "LED", "high": true}}\n
```

| Param | Type | Required | Constraints |
|-------|------|----------|-------------|
| `pin` | string | yes | One of the writable pin names listed above |
| `high` | boolean | yes | `true` = drive high, `false` = drive low |

**Response (`result`):**
```json
{"pin": "LED", "high": true}
```

**Errors:**

| Code | Condition |
|------|-----------|
| `INVALID_PARAMS` | `pin` or `high` absent, unknown pin name, or pin is input-only |
| `HARDWARE_ERROR` | GPIO write failure |

---

### `set_motor_speed`

Set a DC motor's speed as a signed percentage. IPC motor channel indices
map to the configured `[[motors]]` entries (0-based order in `config.toml`).

**Hardware detail:** TC1508S H-bridge — direction pin HIGH = forward,
LOW = reverse; PWM duty sets speed. Channels 12–15, timer group 3 (100 Hz).

**Request:**
```json
{"id": "m1", "method": "set_motor_speed", "params": {"channel": 0, "speed_pct": 50.0, "ttl_ms": 500}}\n
```

| Param | Type | Required | Range | Description |
|-------|------|----------|-------|-------------|
| `channel` | integer | yes | 0–3 | IPC motor index (position in `config.motors`) |
| `speed_pct` | float | yes | −100.0–100.0 | Signed speed: negative = reverse, 0 = stop |
| `ttl_ms` | integer | no | 100–5000 | Lease TTL (ms); motor stopped if not refreshed. Default: 500 |

**Response (`result`):**
```json
{"channel": 0, "speed_pct": 50.0}
```

**Error codes:** `INVALID_PARAMS` if channel is not configured or `speed_pct`
is out of range; `HARDWARE_ERROR` on I2C or GPIO failure.

---

### `stop_all_motors`

Immediately set all configured motor channels to zero duty (stop). Clears all
motor leases.

**Request:**
```json
{"id": "m2", "method": "stop_all_motors", "params": {}}\n
```

**Response (`result`):**
```json
{"stopped": 2}
```

| Field | Type | Description |
|-------|------|-------------|
| `stopped` | integer | Number of motors commanded to stop |

---

### `get_motor_status`

Return the currently active motor TTL leases.

**Request:**
```json
{"id": "m3", "method": "get_motor_status", "params": {}}\n
```

**Response (`result`):**
```json
{
  "active_leases": [
    {"channel": 0, "ttl_remaining_ms": 312, "conn_id": 4},
    {"channel": 1, "ttl_remaining_ms": 198, "conn_id": 4}
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `active_leases` | array | Per-motor lease entries (empty if no active leases) |
| `active_leases[].channel` | integer | IPC motor channel index |
| `active_leases[].ttl_remaining_ms` | integer | Milliseconds until auto-stop |
| `active_leases[].conn_id` | integer | Connection that holds the lease |

---

### `reset_mcu`

Asserts and de-asserts the MCU reset line to restart the Robot HAT V4
microcontroller.

**Hardware detail:** `MCURST` → BCM5 (GPIO output). The procedure is:
1. Set BCM5 low (assert reset)
2. Hold for ≥ 10 ms
3. Set BCM5 high (de-assert)

**Request:**
```json
{"id": "5", "method": "reset_mcu", "params": {}}\n
```

**Response (`result`):**
```json
{"reset_ms": 10}
```

| Field | Type | Description |
|-------|------|-------------|
| `reset_ms` | integer | Duration the reset line was held low (milliseconds) |

---

### `get_servo_status`

Return all currently active servo TTL leases.

**Request:**
```json
{"id": "ss1", "method": "get_servo_status", "params": {}}\n
```

**Response (`result`):**
```json
{
  "active_leases": [
    {"channel": 0, "ttl_remaining_ms": 412, "conn_id": 3}
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `active_leases` | array | Per-channel lease entries (empty if none) |
| `active_leases[].channel` | integer | PWM channel number |
| `active_leases[].ttl_remaining_ms` | integer | Milliseconds until auto-idle |
| `active_leases[].conn_id` | integer | Connection that holds the lease |

---

### `get_mcu_status`

Return MCU reset statistics since daemon start.

**Request:**
```json
{"id": "ms1", "method": "get_mcu_status", "params": {}}\n
```

**Response (`result`):**
```json
{"resets_since_start": 2, "last_reset_s_ago": 47}
```

| Field | Type | Description |
|-------|------|-------------|
| `resets_since_start` | integer | Total `reset_mcu` calls since daemon start |
| `last_reset_s_ago` | integer \| null | Seconds since the last reset; `null` if never reset |

---

### `drive`

Set all configured DC motors to the same speed simultaneously. This is the
preferred way to drive the robot — it is atomic (no inter-motor delay) and
returns the number of motors commanded.

**Request:**
```json
{"id": "d1", "method": "drive", "params": {"speed_pct": 50.0, "ttl_ms": 500}}\n
```

| Param | Type | Required | Range | Description |
|-------|------|----------|-------|-------------|
| `speed_pct` | float | yes | −100.0–100.0 | Signed speed: negative = reverse, 0 = coast |
| `ttl_ms` | integer | no | 100–5000 | Lease TTL (ms); motors stop if not refreshed. Default: 500 |

**Response (`result`):**
```json
{"speed_pct": 50.0, "motors": 2}
```

| Field | Type | Description |
|-------|------|-------------|
| `speed_pct` | float | Commanded speed (echoed) |
| `motors` | integer | Number of motors set |

---

### `steer`

Set the steering servo to a target angle using the channel configured as
`config.servos.steering` (PicarX default: P2).

**Request:**
```json
{"id": "d2", "method": "steer", "params": {"angle_deg": 90.0, "ttl_ms": 500}}\n
```

| Param | Type | Required | Range | Description |
|-------|------|----------|-------|-------------|
| `angle_deg` | float | yes | 0.0–180.0 | Target angle (90° = straight ahead) |
| `ttl_ms` | integer | no | 100–5000 | Lease TTL (ms). Default: 500 |

**Response (`result`):**
```json
{"servo": "steering", "channel": 2, "angle_deg": 90.0, "pulse_us": 1611}
```

| Field | Type | Description |
|-------|------|-------------|
| `servo` | string | Logical servo name (`"steering"`) |
| `channel` | integer | Physical PWM channel used |
| `angle_deg` | float | Commanded angle (echoed) |
| `pulse_us` | integer | Resulting pulse width in µs |

**Error:** Returns `INVALID_PARAMS` if steering servo is not configured
(`config.servos.steering = null`).

---

### `pan_camera`

Set the camera pan (horizontal) servo to a target angle using the channel
configured as `config.servos.camera_pan` (PicarX default: P0).

**Request:**
```json
{"id": "d3", "method": "pan_camera", "params": {"angle_deg": 90.0, "ttl_ms": 500}}\n
```

| Param | Type | Required | Range | Description |
|-------|------|----------|-------|-------------|
| `angle_deg` | float | yes | 0.0–180.0 | Target angle (90° = centre) |
| `ttl_ms` | integer | no | 100–5000 | Lease TTL (ms). Default: 500 |

**Response (`result`):**
```json
{"servo": "camera_pan", "channel": 0, "angle_deg": 90.0, "pulse_us": 1611}
```

**Error:** Returns `INVALID_PARAMS` if camera_pan servo is not configured.

---

### `tilt_camera`

Set the camera tilt (vertical) servo using the channel configured as
`config.servos.camera_tilt` (PicarX default: P1).

**Request:**
```json
{"id": "d4", "method": "tilt_camera", "params": {"angle_deg": 90.0, "ttl_ms": 500}}\n
```

| Param | Type | Required | Range | Description |
|-------|------|----------|-------|-------------|
| `angle_deg` | float | yes | 0.0–180.0 | Target angle (90° = horizontal) |
| `ttl_ms` | integer | no | 100–5000 | Lease TTL (ms). Default: 500 |

**Response (`result`):**
```json
{"servo": "camera_tilt", "channel": 1, "angle_deg": 90.0, "pulse_us": 1611}
```

**Error:** Returns `INVALID_PARAMS` if camera_tilt servo is not configured.

---

### `read_grayscale`

Read all three grayscale sensor ADC channels in a single IPC round-trip.
Channel indices come from `config.sensors.grayscale` (PicarX default: A0, A1, A2).

**Request:**
```json
{"id": "d5", "method": "read_grayscale", "params": {}}\n
```

**Response (`result`):**
```json
{
  "channels": [0, 1, 2],
  "values": [1876, 3421, 892]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `channels` | array[integer] | ADC channel numbers read (from config) |
| `values` | array[integer] | Raw 12-bit ADC readings, one per channel (0–4095) |

---

### `read_ultrasonic`

Trigger the HC-SR04-compatible ultrasonic distance sensor and return the
measured distance. The daemon drives `D2` (BCM 27) as TRIG and reads `D3`
(BCM 22) as ECHO. Pins are configurable via the `[ultrasonic]` config section.

**Request:**
```json
{"id": "u1", "method": "read_ultrasonic", "params": {}}\n
```

**Response (`result`):**
```json
{ "distance_cm": 42.5 }
```

| Field | Type | Description |
|-------|------|-------------|
| `distance_cm` | float | Measured distance in centimetres (valid range: 2–400 cm) |

**Error codes:**

| Code | Meaning |
|------|---------|
| `TIMEOUT` | ECHO pulse did not arrive within `timeout_ms` |
| `NO_ECHO` | Measured distance outside valid range (2–400 cm) |
| `HARDWARE_ERROR` | GPIO bus failure |

---

### `enable_speaker`

Assert the speaker amplifier enable pin (BCM 20, `spk_en`) HIGH, powering the
Robot HAT V4 on-board amplifier. Call this before initiating audio playback via
the Python audio module.

**Request:**
```json
{"id": "s1", "method": "enable_speaker", "params": {}}\n
```

**Response (`result`):**
```json
{ "enabled": true, "pin_bcm": 20 }
```

---

### `disable_speaker`

Assert `spk_en` (BCM 20) LOW, disabling the amplifier. Call after audio
playback completes to conserve power.

**Request:**
```json
{"id": "s2", "method": "disable_speaker", "params": {}}\n
```

**Response (`result`):**
```json
{ "enabled": false, "pin_bcm": 20 }
```

---

### `set_volume`

Set the speaker output volume via the ALSA mixer (`amixer sset`).
The daemon maps `volume_pct` directly to the configured ALSA control
(default: `"Master"` on the card whose `/proc/asound/cards` entry matches
`output_card_name`, default `"sndrpihifiberry"` — the Robot HAT I2S DAC,
which only exists once `dtoverlay=hifiberry-dac` is enabled in the Pi boot
config; no match falls back to the ALSA default card).

**Request:**
```json
{"id": "av1", "method": "set_volume", "params": {"volume_pct": 80}}\n
```

| Param | Type | Required | Constraints |
|-------|------|----------|-------------|
| `volume_pct` | u8 | yes | 0–100 |

**Response (`result`):**
```json
{ "volume_pct": 80 }
```

**Errors:**

| Code | Condition |
|------|-----------|
| `INVALID_PARAMS` | `volume_pct` absent or > 100 |
| `HARDWARE_ERROR` | `amixer` command failed or I/O error |
| `INTERNAL_ERROR` | Failed to parse `amixer` output |

---

### `get_volume`

Read the current output volume from the ALSA mixer.

**Request:**
```json
{"id": "av2", "method": "get_volume", "params": {}}\n
```

**Response (`result`):**
```json
{ "volume_pct": 80 }
```

**Errors:**

| Code | Condition |
|------|-----------|
| `HARDWARE_ERROR` | `amixer` command failed or I/O error |
| `INTERNAL_ERROR` | Failed to parse `amixer` output |

---

### `set_mic_gain`

Set the USB microphone capture gain via the ALSA mixer (`amixer sset`).
The daemon applies `gain_pct` to each configured input ALSA control that
exists on the card (default `input_controls = ["Mic", "Capture"]`; absent
ones are skipped, since USB codecs vary in whether the ADC level is named
`"Mic"` or `"Capture"`). The card is the one whose `/proc/asound/cards` entry
matches `input_card_name` (default `"USB"` — the PCM2902 USB mic, card 1 on
the robot; ALSA card numbers are never hardcoded because they depend on
driver load order). If none of the configured controls exist, the call fails
with an error listing the card's actual controls.

**Request:**
```json
{"id": "mg1", "method": "set_mic_gain", "params": {"gain_pct": 50}}\n
```

| Param | Type | Required | Constraints |
|-------|------|----------|-------------|
| `gain_pct` | u8 | yes | 0–100 |

**Response (`result`):**
```json
{ "gain_pct": 50 }
```

**Errors:**

| Code | Condition |
|------|-----------|
| `INVALID_PARAMS` | `gain_pct` absent or > 100 |
| `HARDWARE_ERROR` | `amixer` command failed or I/O error |
| `INTERNAL_ERROR` | Failed to parse `amixer` output |

---

### `get_mic_gain`

Read the current microphone capture gain from the ALSA mixer.

**Request:**
```json
{"id": "mg2", "method": "get_mic_gain", "params": {}}\n
```

**Response (`result`):**
```json
{ "gain_pct": 50 }
```

**Errors:**

| Code | Condition |
|------|-----------|
| `HARDWARE_ERROR` | `amixer` command failed or I/O error |
| `INTERNAL_ERROR` | Failed to parse `amixer` output |

---

### `get_calibration`

Return a full snapshot of the in-memory calibration store. Values are the
current runtime calibration, which may differ from disk if `save_calibration`
has not been called since the last `set_*_calibration` call.

**Request:**
```json
{"id": "cal1", "method": "get_calibration", "params": {}}\n
```

**Response (`result`):**
```json
{
  "motors": [
    {"channel": 0, "speed_scale": 1.0, "deadband_pct": 0.0, "reversed": false},
    {"channel": 1, "speed_scale": 1.0, "deadband_pct": 0.0, "reversed": false}
  ],
  "servos": {
    "steering":    {"trim_us": 0},
    "camera_pan":  {"trim_us": 0},
    "camera_tilt": {"trim_us": 0}
  },
  "grayscale": [
    {"adc_channel": 0, "white_raw": 100, "black_raw": 3000},
    {"adc_channel": 1, "white_raw": 100, "black_raw": 3000},
    {"adc_channel": 2, "white_raw": 100, "black_raw": 3000}
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `motors[].channel` | integer | IPC motor index (0-based, corresponds to `config.motors` position) |
| `motors[].speed_scale` | float | Multiplier on `speed_pct` before PWM write (0.5–2.0) |
| `motors[].deadband_pct` | float | Minimum `speed_pct` magnitude below which motor stays stopped (0.0–20.0) |
| `motors[].reversed` | boolean | Runtime direction flip (XOR with `MotorConfig.reversed`) |
| `servos` | object | Keys: `"steering"`, `"camera_pan"`, `"camera_tilt"` |
| `servos[].trim_us` | integer | Signed offset (µs) added to computed pulse before 500–2500 clamping |
| `grayscale[].adc_channel` | integer | ADC bus channel number (from `config.sensors.grayscale`) |
| `grayscale[].white_raw` | integer | Raw ADC value captured from a white/reflective surface |
| `grayscale[].black_raw` | integer | Raw ADC value captured from a black/non-reflective surface |

---

### `set_motor_calibration`

Adjust calibration values for one motor channel. Partial updates are accepted —
unspecified fields are left unchanged.

**Request:**
```json
{"id": "cal2", "method": "set_motor_calibration", "params": {"channel": 0, "speed_scale": 1.2, "reversed": true}}\n
```

| Param | Type | Required | Range | Description |
|-------|------|----------|-------|-------------|
| `channel` | integer | yes | 0 to N-1 | IPC motor index |
| `speed_scale` | float | no | 0.5–2.0 | New speed scale multiplier |
| `deadband_pct` | float | no | 0.0–20.0 | New deadband |
| `reversed` | boolean | no | — | New runtime direction flip |

**Response (`result`):**
```json
{"channel": 0, "speed_scale": 1.2, "deadband_pct": 0.0, "reversed": true}
```

**Errors:**

| Code | Condition |
|------|-----------|
| `INVALID_PARAMS` | `channel` absent, ≥ configured motor count, or any value out of range |

---

### `set_servo_calibration`

Set the trim offset (µs) for a named servo. The trim is added to the computed
pulse width before the 500–2500 µs clamp is applied. Calibration may be stored
for a servo that is currently disabled (`None` in config) — it will take effect
if the servo is later enabled.

**Request:**
```json
{"id": "cal3", "method": "set_servo_calibration", "params": {"servo": "steering", "trim_us": -50}}\n
```

| Param | Type | Required | Range | Description |
|-------|------|----------|-------|-------------|
| `servo` | string | yes | `"steering"` \| `"camera_pan"` \| `"camera_tilt"` | Logical servo name |
| `trim_us` | integer | yes | −500–+500 | Signed trim in microseconds |

**Response (`result`):**
```json
{"servo": "steering", "trim_us": -50}
```

**Errors:**

| Code | Condition |
|------|-----------|
| `INVALID_PARAMS` | `servo` absent or not a recognised servo name; `trim_us` outside −500–+500 |

---

### `calibrate_grayscale`

Read the live ADC value for one grayscale sensor and store it as the white or
black surface reference for normalised readings. `channel` is the **sensor
position index** (0 = left, 1 = center, 2 = right per `config.sensors.grayscale`),
not the ADC bus channel number.

**Request:**
```json
{"id": "cal4", "method": "calibrate_grayscale", "params": {"channel": 0, "surface": "white"}}\n
```

| Param | Type | Required | Constraints |
|-------|------|----------|-------------|
| `channel` | integer | yes | 0–2 (sensor position index) |
| `surface` | string | yes | `"white"` or `"black"` |

**Response (`result`):**
```json
{"channel": 0, "adc_channel": 0, "surface": "white", "raw_value": 142, "stored": true}
```

| Field | Type | Description |
|-------|------|-------------|
| `channel` | integer | Sensor position index (echoed) |
| `adc_channel` | integer | ADC bus channel actually read (from `config.sensors.grayscale[channel]`) |
| `surface` | string | Surface name (echoed) |
| `raw_value` | integer | Live ADC reading stored as the reference |
| `stored` | boolean | `true` if the value was committed to the store; `false` if rejected |

**Errors:**

| Code | Condition |
|------|-----------|
| `INVALID_PARAMS` | `channel` outside 0–2; `surface` unrecognised; or storing this value would violate `white_raw < black_raw` |
| `HARDWARE_ERROR` | ADC read failure |

---

### `read_grayscale_normalized`

Read all three grayscale sensors and return per-channel values normalised
against the captured surface calibration. Requires `calibrate_grayscale` to
have been called for both `"white"` and `"black"` surfaces; falls back to
defaults (`white_raw=100`, `black_raw=3000`) if calibration is absent.

**Normalisation formula per channel:**
`normalized = clamp((raw − white_raw) / (black_raw − white_raw), 0.0, 1.0)`
(0.0 = white/reflective, 1.0 = black/non-reflective)

**Request:**
```json
{"id": "cal5", "method": "read_grayscale_normalized", "params": {}}\n
```

**Response (`result`):**
```json
{
  "channels": [0, 1, 2],
  "normalized": [0.04, 0.87, 0.11]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `channels` | array[integer] | ADC channel numbers (from `config.sensors.grayscale`) |
| `normalized` | array[float] | Per-channel normalised values 0.0–1.0 |

**Errors:**

| Code | Condition |
|------|-----------|
| `HARDWARE_ERROR` | ADC read failure |

---

### `save_calibration`

Persist the current in-memory calibration store to disk at `calibration_path`
(default: `/etc/nomopractic/calibration.toml`). Calibration survives daemon
restarts after this call.

**Request:**
```json
{"id": "cal6", "method": "save_calibration", "params": {}}\n
```

**Response (`result`):**
```json
{"saved": true, "path": "/etc/nomopractic/calibration.toml"}
```

| Field | Type | Description |
|-------|------|-------------|
| `saved` | boolean | Always `true` on success |
| `path` | string | Filesystem path the calibration was written to |

**Errors:**

| Code | Condition |
|------|-----------|
| `HARDWARE_ERROR` | Filesystem write failure (permissions, disk full, etc.) |

---

### `reset_calibration`

Revert the in-memory calibration store to factory defaults. The calibration
file on disk is **not** overwritten; call `save_calibration` afterwards to
make the reset permanent across restarts.

**Request:**
```json
{"id": "cal7", "method": "reset_calibration", "params": {}}\n
```

**Response (`result`):**
```json
{"reset": true}
```

---

### `start_routine`

Start a named self-contained hardware routine. The routine runs inside the
daemon as an independent Tokio task and continues after the calling IPC client
disconnects. Motor leases held by the routine are renewed automatically; if the
task stops for any reason, the watchdog idles all motors within `ttl_ms`
milliseconds.

**Currently supported routine names:** `"explore"` — drives forward while
avoiding obstacles (ultrasonic) and cliffs (normalised grayscale sensor).

**Request:**
```json
{"id": "r1", "method": "start_routine", "params": {"name": "explore", "speed_pct": 35.0, "max_duration_s": 120}}\n
```

| Param | Type | Required | Range / Values | Description |
|-------|------|----------|----------------|-------------|
| `name` | string | yes | `"explore"` | Routine to run |
| `speed_pct` | float | no | 1.0–100.0 | Forward drive speed (overrides config default 30.0) |
| `obstacle_threshold_cm` | float | no | > 0 | Ultrasonic distance below which obstacle avoidance triggers (overrides config default 25.0 cm) |
| `cliff_threshold_normalized` | float | no | 0.0–1.0 | Normalised grayscale value at or above which cliff avoidance triggers (overrides config default 0.7) |
| `max_duration_s` | integer | no | > 0 | Auto-stop after this many seconds (overrides config default 300 s) |

Per-call params override `config.toml` defaults for this run only; they are not persisted.

**Response (`result`):**
```json
{"name": "explore", "started_at_uptime_s": 3742}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Routine name (echoed) |
| `started_at_uptime_s` | integer | Daemon uptime in seconds at the moment the routine task was spawned |

**Errors:**

| Code | Condition |
|------|----------|
| `INVALID_PARAMS` | `name` is absent or not a recognised routine name |
| `ALREADY_RUNNING` | A routine is already active; stop it first with `stop_routine` |

---

### `stop_routine`

Stop the currently active routine. Immediately commands all motors to stop.
Returns telemetry statistics for the completed run.

**Request:**
```json
{"id": "r2", "method": "stop_routine", "params": {}}\n
```

**Response (`result`):**
```json
{
  "name": "explore",
  "ran_for_s": 47,
  "obstacles_avoided": 3,
  "cliffs_avoided": 1,
  "stop_reason": "commanded"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Name of the routine that was stopped |
| `ran_for_s` | integer | Seconds elapsed from start to stop |
| `obstacles_avoided` | integer | Number of obstacle avoidance manoeuvres completed |
| `cliffs_avoided` | integer | Number of cliff avoidance manoeuvres completed |
| `stop_reason` | string | `"commanded"` — explicit IPC stop; `"timeout"` — `max_duration_s` elapsed; `"error"` — task panicked |

**Errors:**

| Code | Condition |
|------|----------|
| `INVALID_PARAMS` | No routine is currently running |

---

### `get_routine_status`

Query the current state of the routine engine without affecting it.

**Request:**
```json
{"id": "r3", "method": "get_routine_status", "params": {}}\n
```

**Response (`result`) — idle:**
```json
{"running": false, "name": null, "elapsed_s": null, "obstacles_avoided": null, "cliffs_avoided": null}
```

**Response (`result`) — running:**
```json
{"running": true, "name": "explore", "elapsed_s": 23, "obstacles_avoided": null, "cliffs_avoided": null}
```

> **Phase 11 note:** `obstacles_avoided` and `cliffs_avoided` are always `null`
> while a routine is running in Phase 11. They will become live counters
> (updated each loop iteration) in Phase 12 once task-internal state sharing
> is implemented.

| Field | Type | Description |
|-------|------|-------------|
| `running` | boolean | `true` if a routine task is active |
| `name` | string \| null | Active routine name; `null` when idle |
| `elapsed_s` | integer \| null | Seconds since the routine started; `null` when idle |
| `obstacles_avoided` | integer \| null | Running avoidance count; `null` while running in Phase 11, `null` when idle |
| `cliffs_avoided` | integer \| null | Running cliff avoidance count; `null` while running in Phase 11, `null` when idle |

---

## Safety: Servo & Motor TTL Lease

Servos hold their last commanded position and draw stall current indefinitely
if the controller disappears. Motors would continue spinning uncontrolled.
To prevent this, every `set_servo_*` and `set_motor_speed` command carries a
**TTL (time-to-live)** parameter.

### Daemon Behaviour

1. On receiving a servo command, the daemon sets a per-channel watchdog timer
   to `ttl_ms` milliseconds.
2. If the client refreshes the command before the timer expires, the timer
   resets.
3. If the timer expires (no refresh), the daemon sends a **neutral/idle**
   command to that channel (pulse_us=0, disabling the PWM output).
4. If the **client disconnects** while a servo lease is active, the daemon
   immediately idles all leased channels on that connection.

### Recommended Client Pattern

```python
# Refresh the servo every 200 ms with a 500 ms TTL
while holding_position:
    client.set_servo_angle(channel=0, angle_deg=90.0, ttl_ms=500)
    await asyncio.sleep(0.2)
```

### Rationale

- Prevents runaway servo stall on Python crash or network disconnect
- TTL is short enough (< 1 s) to feel instantaneous on disconnect
- Daemon does not need to know application semantics — TTL is mechanical

---

## Example Session (socat debug)

```bash
# Connect to daemon socket interactively
socat - UNIX-CONNECT:/run/nomopractic/nomopractic.sock

# Type each line and press Enter:
{"id":"1","method":"health","params":{}}\n
# → {"id":"1","ok":true,"result":{"status":"ok","version":"0.1.0",...}}

{"id":"2","method":"get_battery_voltage","params":{}}\n
# → {"id":"2","ok":true,"result":{"voltage_v":7.42,"raw_adc":24700}}

{"id":"3","method":"set_servo_angle","params":{"channel":0,"angle_deg":90.0}}\n
# → {"id":"3","ok":true,"result":{"channel":0,"angle_deg":90.0,"pulse_us":1611}}

{"id":"4","method":"unknown_method","params":{}}\n
# → {"id":"4","ok":false,"error":{"code":"UNKNOWN_METHOD","message":"No method 'unknown_method'"}}
```

---

## Versioning

The IPC schema follows **semantic versioning** independent of nomon and
nomopractic application versions:

| Change | Version bump |
|--------|-------------|
| Add optional field to existing result | Patch |
| Add new method | Minor |
| Remove method, rename field, change type | Major |

The `health` response includes a `schema_version` field that the Python client
checks on connect. The client should reject connections where the major version
of `schema_version` does not match the version it was built against.

---

## Socket Permissions

The daemon creates the socket with mode `0660` and group `nomon`. The `nomon`
Linux user running the Python API must be a member of the `nomon` group:

```bash
# One-time device setup
sudo groupadd -r nomon
sudo usermod -aG nomon pi   # or whatever user runs nomothetic.api
sudo systemctl restart nomopractic
```
