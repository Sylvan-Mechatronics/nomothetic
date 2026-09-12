#!/usr/bin/env bash
# deploy.sh — Deploy nomothetic to the Raspberry Pi over SSH.
#
# Usage:
#   ./scripts/deploy.sh [--local] [--skip-tests] [<version>] [<pi-host>]
#
# Arguments:
#   --local        Deploy the current local source tree (synced via rsync).
#                  Bypasses git fetch/checkout on the Pi. Version is read from
#                  pyproject.toml. Ignored if a version argument is also given.
#   --skip-tests   Skip the 'make test' step on the Pi. Useful for iterating
#                  quickly during development when tests have already passed.
#   version   Git tag to deploy (e.g. "v0.2.0"). If omitted, the script finds
#             and deploys the latest semver tag on the remote. Ignored if --local.
#   pi-host   SSH host (user@host or plain hostname). Overrides NOMON_PI_HOST.
#             If omitted and NOMON_PI_HOST is unset, runs locally — useful
#             when already connected to the Pi via SSH.
#
# Examples:
#   # Deploy local code from a dev machine to the Pi over SSH:
#   ./scripts/deploy.sh --local perceptua@perceptua
#
#   # Deploy latest release from a dev machine to the Pi over SSH:
#   ./scripts/deploy.sh perceptua@perceptua
#
#   # Deploy a specific version from a dev machine to the Pi over SSH:
#   ./scripts/deploy.sh v0.2.0 perceptua@perceptua
#
#   # Deploy local code directly on the Pi (no SSH needed):
#   ./scripts/deploy.sh --local
#
# Environment (read from .env.device or .env.central in the repo root, based on --mode):
#   NOMON_PI_HOST     SSH target — "user@host" or plain hostname. Optional;
#                     if unset the script runs locally.
#   NOMON_SSH_KEY     Path to SSH private key (optional; if set it is passed to
#                     ssh with -i. If unset, SSH may prompt for a password or
#                     use the ssh-agent / default identity.)
#   NOMON_SUDO_PASS   Optional sudo password for non-interactive remote sudo
#                     operations. Leave unset to use interactive sudo prompts.
#   NOMON_REMOTE_DIR  Absolute path to the repo directory on the Pi. Optional;
#                     defaults to ${HOME}/perceptua-nomon/nomothetic.
#
# The script (release mode) connects to the Pi and performs the following steps there:
#   1. Stops all nomothetic servers, including systemd-managed services if present.
#   2. Records the current git ref so it can be restored on failure.
#   3. Fetches tags from origin and checks out the target version.
#   4. Installs Python dependencies (production + dev extras).
#   5. Runs release checks: unit tests only.
#   6. Starts the API server, waits for readiness, starts the stream via the API,
#      performs a health check, then stops the stream and API.
#   7. Installs/updates systemd unit files and restarts the nomothetic services.
#
# The script (--local mode):
#   1. Reads the version from pyproject.toml.
#   2. Syncs the local source tree to the Pi via rsync (skipped if already on Pi).
#   3. Connects to the Pi and performs steps 1, 4–7 above (skipping git operations).
#
# Rollback:
#   If any step from 3–6 fails the script checks out the previous ref,
#   reinstalls production deps, and restarts the previously running services before exiting.
#
# Exit codes:
#   0  Deploy successful.
#   1  Usage / configuration error (no changes made on the Pi).
#   2  Deploy failed; rollback was performed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "${SCRIPT_DIR}")"

# ── Help ───────────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    sed -n '2,60p' "$0" | sed 's/^# \?//'
    exit 0
fi

# ── Pre-scan for --mode (needed before env file loading) ──────────────────────

_mode="device"
_prev=""
for _arg in "$@"; do
    if [[ "${_prev}" == "--mode" ]]; then
        if [[ "${_arg}" == "device" || "${_arg}" == "central" ]]; then
            _mode="${_arg}"
        else
            echo "Error: --mode must be 'device' or 'central', got '${_arg}'" >&2
            exit 1
        fi
    fi
    _prev="${_arg}"
done
unset _prev _arg

# ── Load .env.device or .env.central ──────────────────────────────────────────

ENV_FILE="${REPO_DIR}/.env.${_mode}"
if [[ -f "${ENV_FILE}" ]]; then
    while IFS= read -r line || [[ -n "${line}" ]]; do
        # Strip leading whitespace
        line="${line#"${line%%[![:space:]]*}"}"
        # Skip blank lines and comments
        [[ "${line}" =~ ^# || -z "${line}" ]] && continue
        key="${line%%=*}"
        val="${line#*=}"
        # Strip inline comment, surrounding whitespace, and optional quotes
        val="${val%%#*}"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        val="${val#\"}" ; val="${val%\"}"
        val="${val#\'}" ; val="${val%\'}"
        case "${key}" in
            NOMON_PI_HOST|NOMON_SSH_KEY|NOMON_REMOTE_DIR|NOMON_SUDO_PASS) export "${key}=${val}" ;;
        esac
    done < "${ENV_FILE}"
fi

# Remove CR/LF from optional sudo password in case .env was edited on Windows.
NOMON_SUDO_PASS="$(printf '%s' "${NOMON_SUDO_PASS:-}" | tr -d '\r\n')"
_NOMON_SUDO_PASS_QUOTED="$(printf '%q' "${NOMON_SUDO_PASS}")"

# ── Argument & configuration validation ───────────────────────────────────────

DEPLOY_LOCAL=false
SKIP_TESTS=false
_positional_args=()
_next_is_mode=false

for _arg in "$@"; do
    if [[ "${_next_is_mode}" == true ]]; then
        _next_is_mode=false
        continue  # value already consumed by pre-scan above
    fi
    case "${_arg}" in
        --local)       DEPLOY_LOCAL=true ;;
        --skip-tests)  SKIP_TESTS=true ;;
        --mode)        _next_is_mode=true ;;
        *)             _positional_args+=("${_arg}") ;;
    esac
done

if [[ "${DEPLOY_LOCAL}" == true ]]; then
    VERSION=""
    PI_HOST="${_positional_args[0]:-${NOMON_PI_HOST:-}}"
else
    VERSION="${_positional_args[0]:-}"
    PI_HOST="${_positional_args[1]:-${NOMON_PI_HOST:-}}"
fi

if [[ -n "${VERSION}" && ! "${VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: version must start with 'v' followed by semver (e.g. v0.2.0)" >&2
    exit 1
fi

# ── Local mode: resolve version from pyproject.toml ───────────────────────────

if [[ "${DEPLOY_LOCAL}" == true ]]; then
    _raw_version="$(grep -m1 '^version' "${REPO_DIR}/pyproject.toml" \
        | sed -E 's/.*version\s*=\s*"([^"]+)".*/\1/')"
    if [[ -z "${_raw_version}" ]]; then
        echo "Error: could not determine version from pyproject.toml" >&2
        exit 1
    fi
    VERSION="v${_raw_version}"
    echo "==> Local deploy: nomothetic ${VERSION}"
fi

# ── SSH helpers ────────────────────────────────────────────────────────────────
# If PI_HOST is set we run everything remotely; otherwise we run locally.

if [[ -n "${PI_HOST}" ]]; then
    SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
    if [[ -n "${NOMON_SSH_KEY:-}" ]]; then
        SSH_OPTS+=(-i "${NOMON_SSH_KEY}")
    fi
    echo "==> Deploying nomothetic${VERSION:+ ${VERSION}} → ${PI_HOST}"
    # All deploy params are env vars (not positional args): SSH concatenates
    # positionals into one string and silently drops empty ones, shifting args.
    _VERSION_QUOTED="$(printf '%q' "${VERSION}")"
    _DEPLOY_LOCAL_QUOTED="$(printf '%q' "${DEPLOY_LOCAL}")"
    _REMOTE_DIR_QUOTED="$(printf '%q' "${NOMON_REMOTE_DIR:-}")"
    RUN_CMD=(ssh "${SSH_OPTS[@]}" "${PI_HOST}" "NOMON_SKIP_TESTS=${SKIP_TESTS} NOMON_SUDO_PASS=${_NOMON_SUDO_PASS_QUOTED} NOMON_DEPLOY_VERSION=${_VERSION_QUOTED} NOMON_DEPLOY_LOCAL=${_DEPLOY_LOCAL_QUOTED} NOMON_DEPLOY_REMOTE_DIR=${_REMOTE_DIR_QUOTED} bash -ls")
else
    echo "==> Deploying nomothetic${VERSION:+ ${VERSION}} locally"
    export NOMON_SKIP_TESTS="${SKIP_TESTS}"
    export NOMON_DEPLOY_VERSION="${VERSION}"
    export NOMON_DEPLOY_LOCAL="${DEPLOY_LOCAL}"
    export NOMON_DEPLOY_REMOTE_DIR="${NOMON_REMOTE_DIR:-}"
    RUN_CMD=(bash -ls)
fi

# Deploy-only variables that must NOT be written to the on-device env file.
_DEPLOY_EXCLUDE='^\s*(NOMON_PI_HOST|NOMON_SSH_KEY|NOMON_REMOTE_DIR|NOMON_GITHUB_REPO|NOMON_SUDO_PASS)\s*='

copy_nomothetic_env() {
    if [[ ! -f "${ENV_FILE}" ]]; then
        echo "==> Warning: .env.${_mode} not found; skipping /etc/nomothetic/nomothetic.env creation." >&2
        return
    fi

    local filtered
    filtered="$(grep -vE "${_DEPLOY_EXCLUDE}" "${ENV_FILE}" \
        | grep -vE '^\s*#' \
        | grep -vE '^\s*$')"

    # The copied file is consumed by three parsers: bash `source` (this
    # script's remote block), systemd EnvironmentFile, and start.sh. An
    # unquoted value containing spaces breaks the bash one ("VAR=a b" runs
    # the command `b`), so fail fast here — on the dev machine, before
    # anything is deployed — with the offending lines.
    local _bad_lines
    _bad_lines="$(printf '%s\n' "${filtered}" \
        | grep -nE "^[A-Za-z_][A-Za-z0-9_]*=[^\"'[:space:]][^\"']*[[:space:]]+[^[:space:]]" \
        || true)"
    if [[ -n "${_bad_lines}" ]]; then
        echo "Error: .env.${_mode} contains unquoted values with spaces; double-quote them" >&2
        echo "(VAR=\"a b\") — /etc/nomothetic/nomothetic.env is bash-sourced during deploy:" >&2
        printf '%s\n' "${_bad_lines}" >&2
        exit 1
    fi

    # Autonomy routine start needs a plugin credential in the device env; warn
    # (non-fatal) if neither is set so the operator isn't surprised by a 503.
    if ! printf '%s\n' "${filtered}" \
        | grep -qE '^\s*(NOMON_PLUGIN_KEY|NOMON_PLUGIN_TOKEN)\s*=\s*\S'; then
        echo "==> Warning: no NOMON_PLUGIN_KEY or NOMON_PLUGIN_TOKEN set in .env.${_mode};" >&2
        echo "    autonomy routine start (POST /api/routines/start) will return 503 until one is set." >&2
    fi

    if [[ -n "${PI_HOST}" ]]; then
        echo "==> Writing /etc/nomothetic/nomothetic.env on remote host..."
        local tmp_env_file
        local remote_env_tmp
        tmp_env_file="$(mktemp)"
        remote_env_tmp="/tmp/nomothetic_env.${RANDOM}.$$"
        printf '%s\n' "${filtered}" > "${tmp_env_file}"
        scp "${SSH_OPTS[@]}" "${tmp_env_file}" "${PI_HOST}:${remote_env_tmp}"
        ssh "${SSH_OPTS[@]}" "${PI_HOST}" "NOMON_SUDO_PASS=${_NOMON_SUDO_PASS_QUOTED} REMOTE_ENV_TMP=${remote_env_tmp} bash -s" <<'EO_NOMOTHETIC_ENV'
set -euo pipefail
if [[ -n "${NOMON_SUDO_PASS:-}" ]]; then
    _askpass_script="$(mktemp)"
    chmod 700 "${_askpass_script}"
    cat > "${_askpass_script}" <<EOSUDOPASS
#!/usr/bin/env sh
printf '%s\n' "${NOMON_SUDO_PASS}"
EOSUDOPASS
    export SUDO_ASKPASS="${_askpass_script}"
    trap 'rm -f "${_askpass_script}"' EXIT
    sudo() { command sudo -A "$@"; }
else
    sudo() { command sudo "$@"; }
fi
sudo mkdir -p /etc/nomothetic
sudo mv -f "${REMOTE_ENV_TMP}" /etc/nomothetic/nomothetic.env
sudo chmod 644 /etc/nomothetic/nomothetic.env
EO_NOMOTHETIC_ENV
        rm -f "${tmp_env_file}"
    else
        echo "==> Writing /etc/nomothetic/nomothetic.env locally..."
        sudo mkdir -p /etc/nomothetic
        printf '%s\n' "${filtered}" | sudo tee /etc/nomothetic/nomothetic.env > /dev/null
    fi
}

# ── Local mode: sync source tree to Pi ────────────────────────────────────────

if [[ "${DEPLOY_LOCAL}" == true && -n "${PI_HOST}" ]]; then
    _remote_dir="${NOMON_REMOTE_DIR:-}"
    _remote_dir_for_ssh="${_remote_dir:-~/perceptua-nomon/nomothetic}"
    # We can't expand $HOME for the remote side here, so default to a literal path
    # the remote script will also accept. Use a placeholder that ssh can resolve.
    _rsync_dest="${PI_HOST}:${_remote_dir:-~/perceptua-nomon/nomothetic/}"
    RSYNC_OPTS=(--archive --compress --delete
        --exclude='.git/'
        --exclude='__pycache__/'
        --exclude='*.pyc'
        --exclude='.venv/'
        --exclude='htmlcov/'
        --exclude='logs/'
    )
    if [[ -n "${NOMON_SSH_KEY:-}" ]]; then
        RSYNC_OPTS+=(-e "ssh -i ${NOMON_SSH_KEY} -o StrictHostKeyChecking=accept-new")
    else
        RSYNC_OPTS+=(-e "ssh -o StrictHostKeyChecking=accept-new")
    fi
    echo "==> Ensuring remote deploy directory exists: ${_remote_dir_for_ssh}"
    ssh "${SSH_OPTS[@]}" "${PI_HOST}" "bash -s" -- "${_remote_dir_for_ssh}" <<'EO_MKREMOTE'
set -euo pipefail
_dest="$1"
if [[ "${_dest}" == "~" ]]; then
    _dest="${HOME}"
elif [[ "${_dest}" == ~/* ]]; then
    _dest="${HOME}/${_dest#~/}"
fi
mkdir -p "${_dest}"
EO_MKREMOTE
    echo "==> Syncing local source → ${_rsync_dest}..."
    rsync "${RSYNC_OPTS[@]}" "${REPO_DIR}/" "${_rsync_dest}"
    echo "  Sync complete ✓"
fi

copy_nomothetic_env

# ── Deployment ─────────────────────────────────────────────────────────────────
# All steps below run on the Pi (remote or local) via a single shell session.

"${RUN_CMD[@]}" << 'END_REMOTE'
set -euo pipefail

if [[ -n "${NOMON_SUDO_PASS:-}" ]]; then
    _askpass_script="$(mktemp)"
    chmod 700 "${_askpass_script}"
    cat > "${_askpass_script}" <<EOSUDOPASS
#!/usr/bin/env sh
printf '%s\n' "${NOMON_SUDO_PASS}"
EOSUDOPASS
    export SUDO_ASKPASS="${_askpass_script}"
    trap 'rm -f "${_askpass_script}"' EXIT
    sudo() { command sudo -A "$@"; }
else
    sudo() { command sudo "$@"; }
fi

readonly REQUESTED_VERSION="${NOMON_DEPLOY_VERSION:-}"
readonly DEPLOY_LOCAL="${NOMON_DEPLOY_LOCAL:-false}"
readonly REMOTE_DIR="${NOMON_DEPLOY_REMOTE_DIR:-${HOME}/perceptua-nomon/nomothetic}"
readonly SKIP_TESTS="${NOMON_SKIP_TESTS:-false}"

# ── Resolve target version (pre-flight, before we touch anything) ─────────────

if [[ "${DEPLOY_LOCAL}" == "true" ]]; then
    if [[ ! -d "${REMOTE_DIR}" ]]; then
        echo "Error: ${REMOTE_DIR} does not exist on the Pi." >&2
        exit 1
    fi
    # Version was already resolved from pyproject.toml on the dev machine.
    TARGET="${REQUESTED_VERSION}"
    echo "==> Target: ${TARGET} (local source)"
else
    echo "==> Fresh clone from origin..."
    _github_repo="https://github.com/Sylvan-Mechatronics/nomothetic.git"
    _tmp_clone="$(mktemp -d)"
    git clone --quiet "${_github_repo}" "${_tmp_clone}/nomothetic"

    # Backup existing repo and move fresh clone into place
    if [[ -d "${REMOTE_DIR}" ]]; then
        mv "${REMOTE_DIR}" "${REMOTE_DIR}.backup.$$"
    fi
    mv "${_tmp_clone}/nomothetic" "${REMOTE_DIR}"
    rm -rf "${_tmp_clone}"
    echo "  Clone complete ✓"

    TARGET="${REQUESTED_VERSION}"
    if [[ -z "${TARGET}" ]]; then
        TARGET="$(git -C "${REMOTE_DIR}" tag --list 'v*' --sort=-version:refname | head -1)"
        if [[ -z "${TARGET}" ]]; then
            echo "Error: no semver tags found in the repository." >&2
            exit 1
        fi
        echo "  Latest release tag: ${TARGET}"
    fi

    if [[ ! "${TARGET}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "Error: resolved tag '${TARGET}' is not a valid semver tag." >&2
        exit 1
    fi

    echo "==> Target: ${TARGET}"
fi

cd "${REMOTE_DIR}"

# ── Rollback helper ────────────────────────────────────────────────────────────
# Set up only after pre-flight so that early errors (tag resolution etc.) do
# not trigger a rollback — nothing has been changed on disk at that point.

_ROLLING_BACK=0

rollback() {
    # Guard against re-entry (e.g. if reinstall in rollback also fails).
    [[ "${_ROLLING_BACK}" -eq 1 ]] && exit 2
    _ROLLING_BACK=1

    echo "" >&2
    echo "!! Deployment failed. Rolling back to ${PREV_LABEL:-local}..." >&2

    # In release mode, restore from backup; in local mode, just reinstall.
    if [[ "${DEPLOY_LOCAL}" != "true" ]]; then
        # Find and restore the backup directory if it exists
        for _backup in "${REMOTE_DIR}".backup.*; do
            if [[ -d "${_backup}" ]]; then
                rm -rf "${REMOTE_DIR}"
                mv "${_backup}" "${REMOTE_DIR}"
                echo "  Restored from backup: ${_backup}" >&2
                break
            fi
        done
    fi

    # Stop anything the failed deploy left running — in particular the
    # smoke-test API server: if the readiness probe times out but the process
    # comes up moments later, it would otherwise keep holding port 8443 and
    # the camera, and the systemd service restarted below could never bind.
    ./scripts/stop.sh all 2>&1 || true

    echo "  Reinstalling previous version..." >&2
    uv sync --all-extras --no-extra docs 2>&1 || true

    if [[ "${SYSTEMD_AVAILABLE}" == "true" ]]; then
        if [[ "${PREV_API_SERVICE_ACTIVE}" == "true" ]]; then
            echo "  Restarting nomothetic-api.service..." >&2
            sudo systemctl restart nomothetic-api.service 2>&1 || true
        fi
        if [[ "${PREV_AP_SERVICE_ACTIVE}" == "true" ]]; then
            echo "  Restarting nomothetic-ap.service..." >&2
            sudo systemctl restart nomothetic-ap.service 2>&1 || true
        fi
        if [[ "${PREV_STREAM_SERVICE_ACTIVE}" == "true" ]]; then
            echo "  Restarting nomothetic-stream.service..." >&2
            sudo systemctl restart nomothetic-stream.service 2>&1 || true
        fi
    else
        echo "  Restarting API server..." >&2
        NOMON_API_MODE=device NOMON_DEVICE_AUTH=false ./scripts/start.sh api 2>&1 || true
    fi

    echo "!! Rollback complete. Services restored to ${PREV_LABEL:-local}." >&2
    exit 2
}

trap rollback ERR

# ── Service identity ───────────────────────────────────────────────────────────
# Read NOMON_SERVICE_USER / NOMON_SERVICE_GROUP from the on-device env file if
# it already exists (written by copy_nomothetic_env above), then apply defaults.
# These vars are used by the TLS, systemd, and chown steps below.
# To override, set NOMON_SERVICE_USER / NOMON_SERVICE_GROUP in your local .env
# (they will be written to /etc/nomothetic/nomothetic.env on the Pi).

if [[ -f /etc/nomothetic/nomothetic.env ]]; then
    set -o allexport
    # shellcheck disable=SC1091
    source /etc/nomothetic/nomothetic.env
    set +o allexport
fi
NOMON_SERVICE_USER="${NOMON_SERVICE_USER:-nomon}"
NOMON_SERVICE_GROUP="${NOMON_SERVICE_GROUP:-nomon}"
NOMON_INSTALL_DIR="${REMOTE_DIR}"

# Create the service user/group if they don't already exist.
if ! getent group "${NOMON_SERVICE_GROUP}" >/dev/null 2>&1; then
    echo "==> Creating service group '${NOMON_SERVICE_GROUP}'..."
    sudo groupadd --system "${NOMON_SERVICE_GROUP}"
fi
if ! getent passwd "${NOMON_SERVICE_USER}" >/dev/null 2>&1; then
    echo "==> Creating service user '${NOMON_SERVICE_USER}'..."
    sudo useradd --system --no-create-home --gid "${NOMON_SERVICE_GROUP}" "${NOMON_SERVICE_USER}"
fi

# Add nomon service user to netdev group so nmcli can be called without sudo.
if getent group netdev >/dev/null 2>&1; then
    echo "==> Adding '${NOMON_SERVICE_USER}' to netdev group…"
    sudo usermod -aG netdev "${NOMON_SERVICE_USER}"
else
    echo "WARNING: netdev group not found — nmcli wifi provisioning will not work without sudo"
fi

# Add nomon service user to audio group for /dev/snd — the wake-word listener
# (ADR-021) and the /api/audio/* endpoints capture/play through ALSA devices.
if getent group audio >/dev/null 2>&1; then
    echo "==> Adding '${NOMON_SERVICE_USER}' to audio group…"
    sudo usermod -aG audio "${NOMON_SERVICE_USER}"
else
    echo "WARNING: audio group not found — microphone capture and speaker playback will fail"
fi

# ── Persistent state directory ────────────────────────────────────────────────
# /var/lib/nomon holds device state that MUST survive redeploys: the pairing
# secret (also read by nomopractic as the Wi-Fi Soft AP passphrase) and the device
# JWT signing secret. systemd's StateDirectory=nomon provisions this when the unit
# starts, but we also ensure it here — owned by the service user, before any
# service (re)start — so secret persistence never depends on StateDirectory and
# survives autonomon's deploy, which may create /var/lib/nomon as root when it
# publishes its routine catalogue there. Only the directory is created and
# chowned; existing secret files are left untouched, so the pairing secret stays
# stable across redeploys and is regenerated only on an explicit factory reset.
echo "==> Ensuring persistent state directory /var/lib/nomon..."
sudo mkdir -p /var/lib/nomon
sudo chown "${NOMON_SERVICE_USER}:${NOMON_SERVICE_GROUP}" /var/lib/nomon
sudo chmod 0755 /var/lib/nomon
echo "  State directory ready (owner: ${NOMON_SERVICE_USER}:${NOMON_SERVICE_GROUP}) ✓"

# ── Systemd service state capture ─────────────────────────────────────────────

SYSTEMD_AVAILABLE=false
PREV_API_SERVICE_ACTIVE=false
PREV_AP_SERVICE_ACTIVE=false
PREV_STREAM_SERVICE_ACTIVE=false

if command -v systemctl >/dev/null 2>&1; then
    SYSTEMD_AVAILABLE=true
    for _svc in nomothetic-api nomothetic-ap nomothetic-stream; do
        if sudo systemctl list-unit-files --full --no-legend "${_svc}.service" >/dev/null 2>&1; then
            if sudo systemctl is-active --quiet "${_svc}.service"; then
                if [[ "${_svc}" == "nomothetic-api" ]]; then
                    PREV_API_SERVICE_ACTIVE=true
                elif [[ "${_svc}" == "nomothetic-ap" ]]; then
                    PREV_AP_SERVICE_ACTIVE=true
                else
                    PREV_STREAM_SERVICE_ACTIVE=true
                fi
            fi
            echo "  Stopping ${_svc}.service if it exists..."
            sudo systemctl stop "${_svc}.service" 2>/dev/null || true
        fi
    done
fi

echo "==> Stopping servers..."
./scripts/stop.sh all

# ── Checkout target version (release mode only) ────────────────────────────────

if [[ "${DEPLOY_LOCAL}" != "true" ]]; then
    echo "==> Checking out ${TARGET}..."
    git checkout --quiet "${TARGET}"
fi

# ── System dependencies ───────────────────────────────────────────────────────
# Several Pi extras require native libraries at build time, and the voice
# features need runtime tools:
#   picamera2 → python-prctl  needs libcap-dev
#   picamera2                 needs libcamera-dev, python3-libcamera
#   pyaudio                   needs portaudio19-dev
#   voice STT (ADR-020/021)   needs ffmpeg (decodes voice-clip uploads;
#                             also resamples the TTS echo — ADR-021)
#   wake-word TTS (ADR-021)   needs espeak-ng (speaks the heard transcript)
#   fetch_stt_model.sh        needs unzip (unpacks the Vosk model below)

_sys_pkgs=()
for _pkg in libcap-dev libcamera-dev python3-libcamera portaudio19-dev ffmpeg espeak-ng unzip; do
    if ! dpkg-query -W --showformat='${Status}' "${_pkg}" 2>/dev/null \
            | grep -q "install ok installed"; then
        _sys_pkgs+=("${_pkg}")
    fi
done
if [[ ${#_sys_pkgs[@]} -gt 0 ]]; then
    echo "==> Installing missing system packages: ${_sys_pkgs[*]}..."
    sudo apt-get install -y "${_sys_pkgs[@]}"
    echo "  System packages installed ✓"
else
    echo "==> System packages already present ✓"
fi

# ── Install dependencies ───────────────────────────────────────────────────────

echo "==> Installing dependencies..."
make install-pi

# ── STT model (voice transcription + wake word, ADR-020/021) ──────────────────
# Ensure the Vosk model the config points at is installed — the wake-word
# listener loads it at service start, not lazily. NOMON_STT_MODEL_PATH comes
# from /etc/nomothetic/nomothetic.env (sourced above); unset means the
# nomothetic.stt default. When the configured model is missing, stale
# vosk-model-* trees are removed first so the SD card never hosts two models.
# fetch_stt_model.sh runs under sudo so it never needs to escalate itself
# (the plain `sudo` in a child script would bypass the askpass wrapper).

_stt_model_path="${NOMON_STT_MODEL_PATH:-/var/lib/nomon/stt/vosk-model-small-en-us-0.15}"
_stt_dir="$(dirname "${_stt_model_path}")"
if [[ -d "${_stt_model_path}" && -n "$(ls -A "${_stt_model_path}" 2>/dev/null)" ]]; then
    echo "==> STT model present at ${_stt_model_path} ✓"
elif [[ "$(basename "${_stt_model_path}")" != vosk-model-* ]]; then
    echo "==> WARNING: NOMON_STT_MODEL_PATH (${_stt_model_path}) is missing and does not"
    echo "    look like an official Vosk model directory (vosk-model-*) — install it manually."
else
    echo "==> STT model missing at ${_stt_model_path}; installing..."
    if [[ -d "${_stt_dir}" ]]; then
        for _old_model in "${_stt_dir}"/vosk-model-*; do
            [[ -e "${_old_model}" ]] || continue
            echo "  Removing stale STT model: $(basename "${_old_model}")"
            sudo rm -rf "${_old_model}"
        done
    fi
    sudo env NOMON_STT_MODEL_PATH="${_stt_model_path}" \
        NOMON_SERVICE_GROUP="${NOMON_SERVICE_GROUP}" \
        ./scripts/fetch_stt_model.sh "${_stt_dir}"
    echo "  STT model installed ✓"
fi

# ── Release checks ─────────────────────────────────────────────────────────────

if [[ "${SKIP_TESTS}" == "true" ]]; then
    echo "==> Skipping tests (--skip-tests flag set)."
else
    echo "==> Running tests..."
    make test
fi

# ── Start servers & verify liveness ───────────────────────────────────────────

# Derive the API base URL from config.toml so curl hits the right endpoint.
_api_cfg=$(python3 - config.toml <<'PYEOF'
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]
with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)
a = cfg.get("api", {})
print("NOM_API_PORT=" + str(int(a.get("port", 8443))))
print("NOM_API_USE_SSL=" + str(bool(a.get("use_ssl", True))).lower())
PYEOF
)
eval "${_api_cfg}"
_scheme="$([[ "${NOM_API_USE_SSL}" == "true" ]] && echo "https" || echo "http")"
_api_base="${_scheme}://127.0.0.1:${NOM_API_PORT}"
_curl=(curl -sf -k --max-time 5)

echo "==> Starting API server..."
NOMON_API_MODE=device NOMON_DEVICE_AUTH=false ./scripts/start.sh api

echo "==> Waiting for API to be ready..."
# A cold start on the Pi Zero (imports read from SD, camera init) can take
# well over 30 s — it only looks fast right after 'make test' has warmed the
# page cache. Allow 120 s before declaring failure.
_attempts=0
until "${_curl[@]}" "${_api_base}/" > /dev/null 2>&1; do
    _attempts=$(( _attempts + 1 ))
    if [[ "${_attempts}" -ge 48 ]]; then
        echo "Error: API server did not respond after 120 s." >&2
        exit 1
    fi
    sleep 2.5
done
echo "  API ready ✓"

echo "==> Starting stream server via API..."
_stream_resp="$(curl -sk --max-time 10 \
    -X POST "${_api_base}/api/stream/start" \
    -H "Content-Type: application/json" \
    -d '{}' \
    -w "\n%{http_code}")"
_stream_code="$(printf '%s' "${_stream_resp}" | tail -1)"
_stream_body="$(printf '%s' "${_stream_resp}" | sed '$d')"
if [[ "${_stream_code}" != "200" ]]; then
    echo "Error: failed to start stream server (HTTP ${_stream_code})" >&2
    echo "  Response: ${_stream_body}" >&2
    exit 1
fi
echo "  Stream server started ✓"

echo "==> Health check..."
_health="$("${_curl[@]}" "${_api_base}/")"
_status="$(printf '%s' "${_health}" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))')"
if [[ "${_status}" != "ok" ]]; then
    echo "Error: health check failed — response: ${_health}" >&2
    exit 1
fi
echo "  Health: ${_status} ✓"

echo "==> Stopping stream server via API..."
"${_curl[@]}" -X POST "${_api_base}/api/stream/stop" > /dev/null
echo "  Stream server stopped ✓"

echo "==> Stopping API server..."
./scripts/stop.sh api

# ── TLS certificates ───────────────────────────────────────────────────────────
# Provision TLS certificates via provision_tls_cert(): prefers a Tailscale-issued
# Let's Encrypt cert (browser-trusted), falls back to self-signed.
# Re-runs on every deploy so expiring certs are renewed automatically.

echo "==> Provisioning TLS certificate..."
sudo mkdir -p /etc/nomothetic/tls
_tmp_dir="$(mktemp -d)"
_tmp_cert="${_tmp_dir}/cert.pem"
_tmp_key="${_tmp_dir}/key.pem"
_cert_source="$(.venv/bin/python3 - "${_tmp_cert}" "${_tmp_key}" <<'PYEOF'
import sys
from nomothetic.api import provision_tls_cert
from pathlib import Path
source = provision_tls_cert(Path(sys.argv[1]), Path(sys.argv[2]))
print(source)
PYEOF
)"
sudo mv "${_tmp_cert}" /etc/nomothetic/tls/cert.pem
sudo mv "${_tmp_key}"  /etc/nomothetic/tls/key.pem
rm -rf "${_tmp_dir}"
sudo chmod 640 /etc/nomothetic/tls/key.pem /etc/nomothetic/tls/cert.pem
sudo chown -R "${NOMON_SERVICE_USER}:${NOMON_SERVICE_GROUP}" /etc/nomothetic/tls
echo "  TLS certificate provisioned (source: ${_cert_source}) ✓"

# ── Systemd integration (optional) ────────────────────────────────────────────
# Install and enable systemd service files if systemd is available.

if command -v systemctl >/dev/null 2>&1; then
    _systemd_changed=false

    if ! command -v envsubst >/dev/null 2>&1; then
        echo "Error: envsubst not found. Install: sudo apt-get install -y gettext-base" >&2
        exit 1
    fi
    export NOMON_SERVICE_USER NOMON_SERVICE_GROUP NOMON_INSTALL_DIR

    for _svc_file in systemd/*.service; do
        [[ -f "${_svc_file}" ]] || continue
        _svc_name="$(basename "${_svc_file}")"
        _dest="/etc/systemd/system/${_svc_name}"

        _expanded="$(envsubst '$NOMON_SERVICE_USER $NOMON_SERVICE_GROUP $NOMON_INSTALL_DIR' < "${_svc_file}")"
        if [[ ! -f "${_dest}" ]] || [[ "${_expanded}" != "$(cat "${_dest}")" ]]; then
            echo "  Installing ${_svc_name}..."
            printf '%s\n' "${_expanded}" | sudo tee "${_dest}" > /dev/null
            sudo chmod 644 "${_dest}"
            _systemd_changed=true
        fi
    done

    if [[ "${_systemd_changed}" == "true" ]]; then
        echo "  Reloading systemd daemon..."
        sudo systemctl daemon-reload
    fi

    # Enable and restart the main device-mode service.
    # nomothetic-ap is installed (unit file copied above) but NOT enabled —
    # it is started/stopped exclusively by ap-mode.sh when the Soft AP goes
    # up or down (see nomothetic ADR-015).
    # nomothetic-stream is likewise installed but NOT enabled: streaming is
    # API-managed (POST /api/stream/start runs a token-gated server per run on
    # the same port); the standalone unit is token-less and kept for manual
    # debugging only. Stop + disable it in case an earlier deploy enabled it.
    for _svc in nomothetic-api; do
        if [[ -f "/etc/systemd/system/${_svc}.service" ]]; then
            sudo systemctl enable "${_svc}.service" 2>/dev/null || true
            echo "  Restarting ${_svc}..."
            sudo systemctl restart "${_svc}.service"
        fi
    done
    if [[ -f "/etc/systemd/system/nomothetic-stream.service" ]]; then
        sudo systemctl disable nomothetic-stream.service 2>/dev/null || true
        sudo systemctl stop nomothetic-stream.service 2>/dev/null || true
        echo "  nomothetic-stream.service installed (not boot-enabled; streaming is API-managed) ✓"
    fi
    if [[ -f "/etc/systemd/system/nomothetic-ap.service" ]]; then
        # Ensure it is disabled at boot; ap-mode.sh controls it at runtime.
        sudo systemctl disable nomothetic-ap.service 2>/dev/null || true
        echo "  nomothetic-ap.service installed (not boot-enabled; managed by ap-mode.sh) ✓"
    fi

    echo "  systemd services updated ✓"
else
    echo "  systemd not available — skipping service installation."
fi

echo ""
echo "✓ nomothetic ${TARGET} deployed successfully to ${HOSTNAME}."

# Clean up backup directory from release deploy (if deployment succeeded)
if [[ "${DEPLOY_LOCAL}" != "true" ]]; then
    for _backup in "${REMOTE_DIR}".backup.*; do
        if [[ -d "${_backup}" ]]; then
            rm -rf "${_backup}"
        fi
    done
fi
END_REMOTE
