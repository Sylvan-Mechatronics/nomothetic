# Getting Started

A newcomer guide for cloning the repos, setting up the environment on a
Raspberry Pi, and starting the nomon servers so you can interact with devices
remotely.

---

## What you need

- **Raspberry Pi Zero 2W** (or Pi 4/5) running Raspberry Pi OS bookworm/trixie
- **SunFounder Robot HAT V4** attached (I2C bus 1, address `0x14`)
- **Camera module** attached (CSI ribbon or USB)
- A workstation with SSH access to the Pi

> **Soft AP pairing:** If your Pi is not yet on WiFi, the nomon Soft AP
> watchdog will broadcast a WPA2 hotspot (`nomon-<last4-of-MAC>`) automatically.
> Connect to it with the passphrase shown in the nomothetic startup log, then
> open `http://192.168.4.1:8080` in the nomotactic app to pair
> the device. No Bluetooth required.
> See [pi_setup.md — Wi-Fi Soft AP Pairing](pi_setup.md#8--wi-fi-soft-ap-pairing).

---

## Step 1 — Clone the repositories

On the Pi:

```bash
git clone https://github.com/Perceptua-Nomon/nomopractic.git
git clone https://github.com/Perceptua-Nomon/nomothetic.git
```

---

## Steps 2–5 — Build, deploy, and install

Follow **[pi_setup.md](pi_setup.md)** in full:

1. **Install Rust on the Pi** (or cross-compile from your workstation) — Section: *Installing Rust on the Pi*
2. **Build & deploy nomopractic** — Section: *1 — Build & Deploy nomopractic*
3. **Start the nomopractic daemon** — Section: *1 — Build & Deploy nomopractic → Start the daemon*
4. **Install nomothetic on the Pi** — Section: *3 — Install nomothetic on the Pi*

After completing those steps, verify the daemon is alive:

```bash
echo '{"id":"1","method":"health","params":{}}' \
  | socat - UNIX-CONNECT:/run/nomopractic/nomopractic.sock
# → {"id":"1","ok":true,"result":{"status":"ok","version":"...",...}}
```

---

## Step 6 — Configure the servers

The repo ships a `config.toml` with safe defaults.  Edit it to match your deployment:

```bash
cd nomothetic/
$EDITOR config.toml
```

Key settings in `config.toml`:

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `[stream]` | `host` | `0.0.0.0` | Bind address |
| `[stream]` | `port` | `8000` | HTTP port |
| `[api]` | `host` | `0.0.0.0` | Bind address |
| `[api]` | `port` | `8443` | HTTPS port |
| `[api]` | `use_ssl` | `true` | Auto-generates self-signed cert in `.certs/` |
| `[hat]` | `socket_path` | `/run/nomopractic/nomopractic.sock` | IPC socket |
| `[media]` | `dir` | `~/perceptua-nomon/media` | Base media directory for video and audio |
| `[audio]` | `input_device_index` | unset | Explicit PortAudio mic index (unset = auto-detect by name) |
| `[mqtt]` | `broker` | `""` | MQTT broker address (leave empty to disable telemetry) |
| `[telemetry]` | `device_id` | `""` | Fleet node ID (auto-detected if empty) |

Audio recordings are stored under the directory configured by `[media].dir`
(sub-folder `audio/` is created automatically).  The microphone is auto-detected
by PortAudio device name (default match `"usb"`; tune with
`NOMON_AUDIO_INPUT_NAME`).  Set `[audio].input_device_index` only to force an
explicit PortAudio index — these are not ALSA card numbers, and a wrong value
can crash libportaudio on the Pi.

Sensitive values (SSH keys, JWT secrets, MQTT credentials) belong in mode-specific
env files rather than `config.toml`.  Copy the appropriate example file and fill
in only what you need:

- Device mode: `cp .env.device.example .env.device`
- Central mode: `cp .env.central.example .env.central`

---

## Step 7 — Start the REST API server

The REST API exposes camera control and HAT endpoints over HTTPS.

```bash
make start-api
# or: ./scripts/start.sh api
```

The server starts at **`https://<pi-address>:8443`** and runs in the
background. Its PID is saved to `/tmp/nomothetic-api.pid` and logs go to
`logs/api.log`.

Key endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check |
| `GET` | `/api/camera/status` | Camera + recording state |
| `POST` | `/api/camera/capture` | Capture a still image |
| `POST` | `/api/camera/record/start` | Start video recording |
| `POST` | `/api/camera/record/stop` | Stop video recording |
| `GET` | `/api/hat/battery` | Read battery voltage |
| `POST` | `/api/hat/servo` | Set servo angle |
| `POST` | `/api/hat/reset` | Assert MCU reset |

Interactive API docs are available at `https://<pi-address>:8443/docs`.

> **Self-signed certificate**: your browser and curl will warn about the
> certificate. Pass `-k` / `--insecure` to curl, or import the cert from
> `.certs/cert.pem` into your browser's trust store.

---

## Step 8 — Start the MJPEG streaming server

The streaming server serves a live camera feed viewable in any browser.

```bash
make start-stream
# or: ./scripts/start.sh stream
```

Open **`http://<pi-address>:8000`** in a browser to watch the live feed.

The `/stream` endpoint serves raw MJPEG and can be consumed by any MJPEG
client (VLC, ffmpeg, OpenCV `VideoCapture`, etc.):

```bash
# VLC
vlc http://<pi-address>:8000/stream

# OpenCV
cap = cv2.VideoCapture("http://<pi-address>:8000/stream")
```

---

## Running both servers

Start and stop both servers together:

```bash
make start-stream
make start-api

make stop           # stop both
make stop-stream    # stop stream only
make stop-api       # stop API only
```

Or using the scripts directly (supports `--foreground` and `--config` flags):

```bash
./scripts/start.sh stream
./scripts/start.sh api
./scripts/stop.sh all
```

Use `--foreground` when debugging — Ctrl-C stops the server:

```bash
./scripts/start.sh api --foreground
```

---

## Quick remote test

Once both servers are running, from your workstation:

```bash
PI=<pi-address>

# Health check
curl -sk https://$PI:8443/ | python3 -m json.tool

# Battery voltage
curl -sk https://$PI:8443/api/hat/battery | python3 -m json.tool

# Capture a still image
curl -sk -X POST https://$PI:8443/api/camera/capture \
  -H 'Content-Type: application/json' \
  -d '{"filename": "test.jpg"}' | python3 -m json.tool

# Live stream in browser
open http://$PI:8000    # macOS; use xdg-open on Linux
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Connection refused` on port 8443 | API server not running — `make start-api`; check `logs/api.log` |
| `Connection refused` on port 8000 | Streaming server not running — `make start-stream`; check `logs/stream.log` |
| `503 Service Unavailable` from HAT endpoint | nomopractic daemon not running — `sudo systemctl start nomopractic` |
| `HARDWARE_ERROR` from battery/servo | HAT not connected or I2C not detected — `sudo i2cdetect -y 1` should show `0x14` |
| Soft AP not appearing | Watchdog service not running — `sudo systemctl status nomon-softap-watchdog.timer`; check connectivity with `nmcli general connectivity` |
| Certificate warning in browser | Expected for self-signed certs — click through or import `.certs/cert.pem` |

For deeper troubleshooting, see [pi_setup.md](pi_setup.md#troubleshooting).
