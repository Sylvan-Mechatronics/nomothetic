# Raspberry Pi Setup

Comprehensive guide for reimaging, provisioning, deploying, and validating a
nomon device on Raspberry Pi.

This document covers:

- Fresh Pi reimage and first boot hardening
- Build/deploy workflow for nomographic, nomopractic, and nomothetic
- AP mode and WiFi mode TLS behavior (including AP self-signed certs)
- Verification commands for certs, pairing secret, JWT signer secret, and services

## Prerequisites

### Hardware

- Raspberry Pi Zero 2W (or compatible Pi)
- MicroSD card (16 GB minimum recommended)
- SunFounder Robot HAT V4 attached (I2C bus 1, address `0x14`)

### Software and Accounts

- Raspberry Pi Imager available on your dev machine
- Access to Raspberry Pi Connect and SSH
- GitHub access to the nomon repos
- Optional: Tailscale account for remote trusted HTTPS access

### Repositories

Clone/update the monorepo on your Pi (or clone each repo separately):

- `nomographic`
- `nomopractic`
- `nomothetic`

---

## 1 - Reimage the Pi

1. Power down the Pi and remove the SD card.
2. Reimage with Raspberry Pi Imager.
3. In the imager advanced options, enable:
   - SSH
   - Raspberry Pi Connect
4. Reinsert the SD card and boot the Pi.

After first boot, connect via Raspberry Pi Connect remote shell.

---

## 2 - First-Boot Access and Build Tooling

### 2.1 Optional: install and configure Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale set --operator="$USER"
```

### 2.2 Configure SSH key access

On the Pi (replace placeholders):

```bash
PI_USER=<pi_user>
DEV_KEY="ssh-ed25519 <dev_public_key> <dev_user>@<dev_machine>"

mkdir -p /home/$PI_USER/.ssh
chmod 700 /home/$PI_USER/.ssh
printf '%s\n' "$DEV_KEY" >> /home/$PI_USER/.ssh/authorized_keys
chmod 600 /home/$PI_USER/.ssh/authorized_keys
sudo chown -R $PI_USER:$PI_USER /home/$PI_USER/.ssh
```

Then remove any old SSH keys & connect from your dev machine:

```bash
ssh-keygen -f '~/.ssh/known_hosts' -R '<pi_host>'
ssh <pi_user>@<pi_host>
```

### 2.3 Configure temporary swap for Rust builds (8 GiB)

```bash
sudo mkdir -p /etc/rpi/swap.conf.d/

sudo tee /etc/rpi/swap.conf.d/80-rust-build.conf > /dev/null <<'EOF'
[Main]
Mechanism=swapfile

[File]
FixedSizeMiB=8192
EOF

sudo reboot && exit
```

After reboot:

```bash
free -h
```

### 2.4 Install Rust

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
rustc --version
```

### 2.5 Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv --version
```

### 2.6 Remove temporary build swap (optional)

If you are done compiling on the Pi:

```bash
sudo rm -f /etc/rpi/swap.conf.d/80-rust-build.conf
sudo reboot && exit
```

### 2.7 Install Docker (required for nomographic local DB service)

`nomographic`'s local DB (`nomographic-local-db.service`, deployed via
`nomographic/scripts/deploy-local.sh` / `make deploy-local`) runs ArcadeDB in
a container. Debian trixie ships a current `docker.io` build in its own repos
— no need for the upstream `get.docker.com` convenience script:

```bash
sudo apt-get update
sudo apt-get install -y docker.io
```

Add the deploy SSH user to the `docker` group so `nomographic`'s migration
scripts (which run without `sudo`) can reach the daemon socket. Group
membership only takes effect on a new login session, so reconnect (or
`newgrp docker`) afterward:

```bash
sudo usermod -aG docker "$USER"
# then start a fresh SSH session, or:
newgrp docker
```

Verify:

```bash
docker --version
sudo systemctl is-active docker
docker run --rm hello-world
```

If you skip the group step, `deploy-local.sh`'s migration step fails with
`permission denied while trying to connect to the Docker daemon socket`.

---

## 3 - Prepare Runtime Users, Groups, and Paths

Run on the Pi:

```bash
sudo groupadd -f nomon
sudo usermod -aG nomon "$USER"

sudo mkdir -p /run/nomopractic
sudo chown root:nomon /run/nomopractic

# Re-login so your group membership is refreshed
newgrp nomon
```

Get the AP SSID suffix (`last4` of wlan0 MAC):

```bash
cat /sys/class/net/wlan0/address | tr -d ':' | tr '[:upper:]' '[:lower:]' | grep -o '.\{4\}$'
```

### 3.1 Sudo for deploys without a stored password (DRAFT)

> **Draft, not yet applied or tested.** Goal: stop storing `NOMON_SUDO_PASS`
> in plaintext `.env*` files on the dev machine.

The deploy scripts need broad root access: they write units into
`/etc/systemd/system`, install `/etc/sudoers.d` and polkit rules, run
`apt-get`, and run venv Python as root. A sudoers allowlist that permits all
of that is still root-equivalent, so pick one of these instead of pretending
to scope the deploy:

- **Option A: keep the sudo password, but don't store it.** Leave
  `NOMON_SUDO_PASS` unset and enter it when deploying. `lib/common.sh` already
  falls back to `ssh -t` plus an interactive `sudo` prompt when it is unset.
  The per-repo `scripts/deploy.sh` files send their remote script over ssh
  stdin, so a typed prompt probably won't work there without script changes.
  Test that first. Add the scoped rule below so routine restarts don't need
  the password.
- **Option B: passwordless sudo for the deploy user.** Your Pi login is then
  protected only by the SSH key, so require key-only SSH and a passphrase on
  the key. That's no weaker than today: a password stored next to the SSH key
  on the dev machine adds nothing once the dev machine is compromised.

Check what is already there first. Some Raspberry Pi OS images ship
`010_pi-nopasswd`:

```bash
sudo ls -l /etc/sudoers.d/
command -v systemctl journalctl   # confirm paths used below (/usr/bin on Bookworm)
```

Read service logs through a group instead of sudo, for either option:

```bash
sudo usermod -aG systemd-journal "$USER"   # journalctl -u <unit> without sudo
```

#### Option A: scoped rule for routine service control

Replace `DEPLOY_USER` with your Pi login user (`echo "$USER"`). Arguments are
matched literally, so use the `.service` suffix when running these.

```bash
sudo visudo -f /etc/sudoers.d/nomon-service-control
```

```sudoers
# /etc/sudoers.d/nomon-service-control
# Passwordless start/stop/restart of nomon units only. Cannot create or
# modify units, so this is not root-equivalent.
Cmnd_Alias NOMON_SVC = \
    /usr/bin/systemctl start   nomothetic-api.service, \
    /usr/bin/systemctl stop    nomothetic-api.service, \
    /usr/bin/systemctl restart nomothetic-api.service, \
    /usr/bin/systemctl start   nomothetic-ap.service, \
    /usr/bin/systemctl stop    nomothetic-ap.service, \
    /usr/bin/systemctl restart nomothetic-ap.service, \
    /usr/bin/systemctl start   nomothetic-stream.service, \
    /usr/bin/systemctl stop    nomothetic-stream.service, \
    /usr/bin/systemctl restart nomothetic-stream.service, \
    /usr/bin/systemctl start   nomopractic.service, \
    /usr/bin/systemctl stop    nomopractic.service, \
    /usr/bin/systemctl restart nomopractic.service, \
    /usr/bin/systemctl start   nomographic-local-db.service, \
    /usr/bin/systemctl stop    nomographic-local-db.service, \
    /usr/bin/systemctl restart nomographic-local-db.service

DEPLOY_USER ALL=(root) NOPASSWD: NOMON_SVC
```

#### Option B: passwordless deploys

```bash
sudo visudo -f /etc/sudoers.d/nomon-deploy
```

```sudoers
# /etc/sudoers.d/nomon-deploy
# Root-equivalent by design: deploys install units, sudoers and polkit rules.
DEPLOY_USER ALL=(ALL) NOPASSWD: ALL
```

Then disable SSH password logins so the key is the only way in:

```bash
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' \
  | sudo tee /etc/ssh/sshd_config.d/10-nomon-keys-only.conf
sudo sshd -t && sudo systemctl reload ssh
```

Keep your current SSH session open and confirm a new key-based login works
before closing it.

#### Either option: verify, then remove the stored password

```bash
sudo visudo -c                      # all sudoers files parse
sudo -l                             # shows the new rule
```

On the dev machine, delete the `NOMON_SUDO_PASS=` line from every env file that
has it (`autonomon/.env.device`, `nomopractic/.env.device`,
`nomothetic/.env.device`, `nomothetic/.env.central`, `nomographic/.env.local`),
run one deploy to confirm, then change the Pi user's password (`passwd`),
since the old one has been stored in plaintext.

---

## 4 - Configure Environment Files

Configure `.env` or deployment env files for local/device mode as required by
your deployment scripts.

For nomothetic:

- Keep your WiFi/Tailscale TLS host configuration up to date.
- Add the AP suffix result from step 3 where your local env expects it.
- `NOMON_TLS_EXTRA_HOSTS` applies to the WiFi-mode cert provisioning path.
  AP-mode cert SANs are fixed to `192.168.4.1`, `127.0.0.1`, and `localhost`
  and do not depend on `NOMON_TLS_EXTRA_HOSTS`.

---

## 5 - Deploy Services

From each repo root on the Pi (recommended order):

```bash
cd ~/perceptua-nomon/nomographic
make deploy-local

cd ~/perceptua-nomon/nomopractic
make deploy-local

cd ~/perceptua-nomon/nomothetic
make deploy-local

cd ~/perceptua-nomon/autonomon
make deploy-local
```

If your layout differs, use the equivalent repository paths.

### 5.1 Optional: voice-command transcription (STT)

`POST /api/ai/transcribe` recognises speech on-device so the app's command bar
can take voice input. It needs ffmpeg and a local Vosk model; without them the
endpoint returns 503 and everything else works normally.

**`make deploy-local` handles both automatically** (Phase 29): it apt-installs
ffmpeg/unzip alongside the other system packages, and installs the Vosk model
`NOMON_STT_MODEL_PATH` points at (default small English) — removing any stale
`vosk-model-*` trees first, so pointing the config at a different model and
redeploying swaps it in one step. The manual equivalent, for standalone use:

```bash
sudo apt install -y ffmpeg unzip

cd ~/perceptua-nomon/nomothetic
make fetch-stt-model          # fetches the model NOMON_STT_MODEL_PATH names
                              # (default: small English, ~40 MB)
sudo systemctl restart nomothetic-api
```

The model loads lazily on the first transcription request (a few seconds, and
a large slice of the Pi Zero 2W's RAM — see ADR-020). Verify with:

```bash
curl -sk -X POST https://localhost:8443/api/ai/transcribe \
  -H "Authorization: Bearer <device-jwt>" \
  -F "audio=@clip.wav"
```

### 5.2 Optional: wake-word voice commands ("hey nomon")

The robot listens on its own USB mic for a catch phrase, chimes, captures the
spoken command, and runs it through the AI relay (ADR-021). Prerequisites: the
§5.1 STT setup (vosk + model), the `[audio]` extra (pyaudio — installed by the
standard deploy), an Anthropic key (`PUT /api/ai/key` or `ANTHROPIC_API_KEY`),
and the service user in the `audio` group (deploy.sh ≥ Phase 29 does this;
older installs: `sudo usermod -aG audio nomon && sudo systemctl restart
nomothetic-api`).

While a command is dispatched the robot also **speaks the heard transcript
back** through the speaker (concurrently with the AI call, filling the silent
gap). This needs `espeak-ng` — apt-installed by the deploy alongside ffmpeg,
manually `sudo apt install -y espeak-ng`. Without it the spoken echo is simply
skipped; the processing chime still plays. Tune the voice/rate with
`NOMON_TTS_VOICE` / `NOMON_TTS_RATE_WPM` (see `.env.device.example`).

Enable it in `/etc/nomothetic/nomothetic.env` (or `.env.device` for the
manual `start.sh` path). Values with spaces must be double-quoted — the file
is bash-sourced during deploys as well as read by systemd:

```bash
NOMON_WAKE_PHRASE="hey nomon"
NOMON_WAKE_PHRASE_VARIANTS="hey no man,hey no mon"
```

then `sudo systemctl restart nomothetic-api`. The journal shows
`wake-word listener started (phrase="hey nomon", ...)` and the chime files
appear under `media/audio/chimes/` (replace them to customise the sounds).

**Tuning the phrase (important):** Vosk silently drops grammar words that are
missing from its vocabulary — "nomon" is one — so the literal phrase may never
match and the *variants* are what actually fire. Tune live without restarts:

```bash
# Status (state, phrase, variants):
curl -sk https://localhost:8443/api/voice/wake -H "Authorization: Bearer <device-jwt>"

# Try a different variant set (in-memory; persist winners in the env file):
curl -sk -X PUT https://localhost:8443/api/voice/wake \
  -H "Authorization: Bearer <device-jwt>" -H "Content-Type: application/json" \
  -d '{"phrase": "hey nomon", "variants": ["hey no man", "hey no mon"], "enabled": true}'
```

Say the phrase, watch `journalctl -u nomothetic-api -f` for
`wake phrase detected` / `wake command heard`, and adjust. A quiet room may
also need a lower `NOMON_WAKE_RMS_THRESHOLD` (the silence gate) — set it to 0
to rule the gate out while tuning.

---

## 6 - Service Health Verification

### 6.1 Check core services

```bash
sudo systemctl daemon-reload

sudo systemctl status nomopractic
sudo systemctl status nomothetic-api
sudo systemctl status nomon-softap-watchdog.timer
```

If AP is manually activated, also check:

```bash
sudo systemctl status nomothetic-ap
```

### 6.2 Verify nomopractic socket and basic IPC

```bash
sudo apt install -y socat

echo '{"id":"1","method":"health","params":{}}' \
  | socat - UNIX-CONNECT:/run/nomopractic/nomopractic.sock
```

### 6.3 Verify nomothetic HTTPS API

```bash
curl -sk https://localhost:8443/
```

You can also open:

- `https://<pi_host>:8443/docs`

---

## 7 - AP Mode and Pairing Flow (Current Behavior)

AP and WiFi modes are separated:

- WiFi mode uses browser-trusted HTTPS when available (for example, Tailscale cert path).
- AP mode binds to `192.168.4.1:8080` using **plain HTTP** (interface-scoped; no TLS required).
- No bootstrap or certificate delivery service — the mobile client connects directly over plain HTTP.

### 7.1 Trigger AP mode manually

```bash
sudo /usr/local/bin/ap-mode.sh up
```

Expected:

- `nomon-ap` hotspot appears
- `nomothetic-ap` starts (`192.168.4.1:8080`)

### 7.2 Pairing secret and AP passphrase

```bash
sudo cat /var/lib/nomon/pairing_secret   # 8-digit code: typed into the app when pairing over the home network
sudo cat /var/lib/nomon/ap_passphrase    # 20-char WPA2 passphrase: entered once in the phone's Wi-Fi settings
```

The two are deliberately different (review finding S-1): a short numeric WPA2
PSK can be recovered offline from a captured handshake, so the hotspot uses its
own long secret. Both are also mirrored to `/run/nomothetic/` at startup.

### 7.3 AP health check

```bash
curl -s http://192.168.4.1:8080/api/health
curl -s http://192.168.4.1:8080/api/device/auth/status
```

### 7.4 Verify AP binding is interface-scoped

```bash
sudo ss -tlnp | grep ':8080'
```

Expected:

- `192.168.4.1:8080` (AP API HTTP)
- No listener on `0.0.0.0:8080`

### 7.5 Disable AP mode

```bash
sudo /usr/local/bin/ap-mode.sh down
```

---

## 8 - Secret and JWT Provisioning Verification

These checks confirm that pairing and JWT signer state are provisioned correctly.

### 8.0 Verify device/WiFi TLS cert files (systemd device API service)

The device API service (`nomothetic-api`) serves HTTPS using:

- `/etc/nomothetic/tls/cert.pem`
- `/etc/nomothetic/tls/key.pem`

Check presence and permissions:

```bash
sudo ls -l /etc/nomothetic/tls/
sudo stat -c '%n %a %U:%G' /etc/nomothetic/tls/cert.pem /etc/nomothetic/tls/key.pem
```

Inspect certificate identity and SAN:

```bash
sudo openssl x509 -in /etc/nomothetic/tls/cert.pem -noout -subject -issuer
sudo openssl x509 -in /etc/nomothetic/tls/cert.pem -noout -ext subjectAltName
sudo openssl x509 -in /etc/nomothetic/tls/cert.pem -noout -fingerprint -sha256
```

Confirm the live endpoint presents this cert:

```bash
echo | openssl s_client -connect localhost:8443 -servername localhost 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha256
```

The fingerprint from the live endpoint should match the fingerprint from
`/etc/nomothetic/tls/cert.pem`.

### 8.1 Verify pairing secret file

```bash
sudo ls -l /var/lib/nomon/pairing_secret
sudo stat -c '%n %a %U:%G' /var/lib/nomon/pairing_secret
sudo wc -c /var/lib/nomon/pairing_secret
```

### 8.2 Verify persistent device JWT signer secret

```bash
sudo ls -l /var/lib/nomon/device_jwt_secret
sudo stat -c '%n %a %U:%G' /var/lib/nomon/device_jwt_secret
sudo wc -c /var/lib/nomon/device_jwt_secret
```

Expected:

- File exists after nomothetic startup
- Mode `0600`
- Non-trivial length (at least 32 characters)

### 8.3 Verify signer persistence across restart

```bash
sudo sha256sum /var/lib/nomon/device_jwt_secret
sudo systemctl restart nomothetic-api
sleep 2
sudo sha256sum /var/lib/nomon/device_jwt_secret
```

Expected: hash remains unchanged unless an explicit key rotation/reset occurred.

### 8.4 Verify startup logs for secret provisioning

```bash
sudo journalctl -u nomothetic-api -n 200 --no-pager | grep -Ei 'pairing secret|jwt secret|ap cert|bootstrap'
```

---

## 9 - End-to-End Device Validation

1. Run nomotactic and connect to the device.
2. Complete pairing flow.
3. Confirm protected endpoints reject unauthenticated requests and succeed with bearer tokens.
4. Exercise camera/sensor/motor commands.
5. If testing AP mode:
   - trigger AP mode with `ap-mode.sh up`
   - connect mobile to `nomon-ap` hotspot
   - pair and run commands over AP HTTP (`http://192.168.4.1:8080`)

Useful manual checks:

```bash
# Unauthorized should fail
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.4.1:8080/api/sensor/grayscale

# Pair status
curl -s http://192.168.4.1:8080/api/device/auth/status
```

---

## 10 - Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `Permission denied` on `/run/nomopractic/nomopractic.sock` | User not in `nomon` group | `sudo usermod -aG nomon $USER` then re-login |
| AP service fails to start | NetworkManager state issue | Check `journalctl -u nomothetic-ap -n 200` and `ip addr show wlan0` |
| `http://192.168.4.1:8080` unreachable | AP API service down or AP interface not up | `sudo systemctl status nomothetic-ap` and `ip addr show wlan0` |
| Re-pair required after reboot | JWT signer not persisted | Validate `/var/lib/nomon/device_jwt_secret` presence and mode |
| Commands return hardware errors | HAT/I2C unavailable | `sudo i2cdetect -y 1` should include `0x14` |
| Pi becomes slow/unresponsive (high ping latency, SSH timeouts) while `nomographic-local-db` or its migrator container runs | Memory/swap thrashing — the Pi Zero 2W has ~415 MiB usable RAM, and an ArcadeDB JVM (`-Xmx384m` by default) can exhaust it alone, let alone two running at once | Check `free -h` for high swap usage; see §10.1 to add persistent swap. Also confirm `nomographic`'s `LOCAL_MIGRATOR_USE_RUNNING_SERVICE=1` path is actually taking effect during deploy (a stray temporary migrator container running alongside the persistent service is the usual second-JVM cause) |

### 10.1 Add persistent swap (memory pressure under ArcadeDB / low-memory services)

The temporary 8 GiB build swap in §2.3 is meant to be removed after compiling
(§2.6) — it's oversized for always-on use and not intended to persist. For
ongoing memory pressure from long-running services (e.g. `nomographic-local-db`),
add a smaller **persistent** swapfile using the same mechanism:

```bash
sudo mkdir -p /etc/rpi/swap.conf.d/

sudo tee /etc/rpi/swap.conf.d/50-persistent.conf > /dev/null <<'EOF'
[Main]
Mechanism=swapfile

[File]
FixedSizeMiB=1024
EOF

sudo reboot && exit
```

After reboot, confirm it's active:

```bash
free -h
swapon --show
```

Notes:

- Pick a filename that sorts before `80-rust-build.conf` (e.g. `50-`) so the
  persistent config isn't accidentally deleted by the §2.6 cleanup step,
  which only removes `80-rust-build.conf`.
- 1024 MiB is a starting point for easing swap thrashing on a 415 MiB-RAM Pi
  Zero 2W; adjust `FixedSizeMiB` based on observed pressure in `free -h`.
- The microSD card has limited write endurance — persistent swap trades some
  card lifespan for stability. This does not replace fixing an underlying
  cause (e.g. two ArcadeDB containers running concurrently); use it alongside
  the root-cause fix, not instead of it.

---

## Further Reading

- [getting_started.md](getting_started.md)
- [integration-testing-plan.md](integration-testing-plan.md)
- [hat_ipc_schema.md](hat_ipc_schema.md)
- [hat_python_client.md](hat_python_client.md)
- [architecture.md](architecture.md)
- [pi_hardware.md](pi_hardware.md)
