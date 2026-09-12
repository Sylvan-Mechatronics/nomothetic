#!/usr/bin/env bash
# Start a nomothetic server in the background.
#
# Usage:
#   ./scripts/start.sh <stream|api|all> [OPTIONS]
#
# Arguments:
#   stream   Start the MJPEG stream server (Flask, HTTP).
#   api      Start the REST API server (FastAPI/uvicorn, HTTPS).
#   all      Start both the stream and API servers.
#
# Options:
#   --mode device|central
#                   Which env file to load: .env.device (default) or .env.central.
#   --config FILE   Path to TOML config file.
#                   Defaults to ./config.toml, then <repo-root>/config.toml.
#   --foreground    Run in the foreground instead of backgrounding (useful
#                   for debugging – Ctrl-C to stop). Not supported with 'all'.
#   -h, --help      Show this help and exit.
#
# The server PID is written to /tmp/nomothetic-<type>.pid.
# Stop it with:
#   ./scripts/stop.sh <stream|api|all>
# or:
#   kill $(cat /tmp/nomothetic-<type>.pid)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "${SCRIPT_DIR}")"

CONFIG_FILE=""
FOREGROUND=false
MODE="device"

# ─── Parse server type (required first arg) ───────────────────────────────────
if [[ $# -eq 0 || "$1" == "-h" || "$1" == "--help" ]]; then
  sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

SERVER_TYPE="$1"
shift

if [[ "${SERVER_TYPE}" != "stream" && "${SERVER_TYPE}" != "api" && "${SERVER_TYPE}" != "all" ]]; then
  echo "Error: server type must be 'stream', 'api', or 'all', got '${SERVER_TYPE}'." >&2
  echo "Run '$(basename "$0") --help' for usage." >&2
  exit 1
fi

# ─── Parse optional arguments ─────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --mode)
      if [[ "${2:-}" != "device" && "${2:-}" != "central" ]]; then
        echo "Error: --mode must be 'device' or 'central', got '${2:-}'" >&2
        exit 1
      fi
      MODE="$2"
      shift 2
      ;;
    --config)
      CONFIG_FILE="$2"
      shift 2
      ;;
    --foreground)
      FOREGROUND=true
      shift
      ;;
    -h|--help)
      sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Error: unknown argument: $1" >&2
      echo "Run '$(basename "$0") --help' for usage." >&2
      exit 1
      ;;
  esac
done

# ─── Handle 'all' by delegating to stream + api ──────────────────────────────
if [[ "${SERVER_TYPE}" == "all" ]]; then
  if [[ "${FOREGROUND}" == "true" ]]; then
    echo "Error: --foreground is not supported with 'all'; use 'stream' or 'api' directly." >&2
    exit 1
  fi
  FORWARD_ARGS=(--mode "${MODE}")
  [[ -n "${CONFIG_FILE}" ]] && FORWARD_ARGS+=(--config "${CONFIG_FILE}")
  "$0" stream "${FORWARD_ARGS[@]}"
  "$0" api    "${FORWARD_ARGS[@]}"
  exit 0
fi

PID_FILE="/tmp/nomothetic-${SERVER_TYPE}.pid"

# ─── Locate config file ───────────────────────────────────────────────────────
if [[ -z "${CONFIG_FILE}" ]]; then
  if [[ -f "${PWD}/config.toml" ]]; then
    CONFIG_FILE="${PWD}/config.toml"
  elif [[ -f "${REPO_DIR}/config.toml" ]]; then
    CONFIG_FILE="${REPO_DIR}/config.toml"
  else
    echo "Error: config.toml not found." >&2
    echo "  Expected at: ${PWD}/config.toml or ${REPO_DIR}/config.toml" >&2
    exit 1
  fi
fi

# ─── Load .env.device or .env.central defaults without overriding explicit env vars ─
if [[ -f "${REPO_DIR}/.env.${MODE}" ]]; then
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ "${line}" =~ ^# || -z "${line}" ]] && continue

    key="${line%%=*}"
    val="${line#*=}"

    val="${val%%#*}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    val="${val#\"}" ; val="${val%\"}"
    val="${val#\'}" ; val="${val%\'}"

    # Keep explicitly provided environment values (e.g. deploy-time overrides).
    if [[ -z "${!key+x}" ]]; then
      export "${key}=${val}"
    fi
  done < "${REPO_DIR}/.env.${MODE}"
fi

# ─── Activate virtual environment if present ─────────────────────────────────
VENV_ACTIVATE="${REPO_DIR}/.venv/bin/activate"
if [[ -f "${VENV_ACTIVATE}" ]]; then
  # shellcheck source=/dev/null
  source "${VENV_ACTIVATE}"
fi

# ─── Parse TOML config via Python ────────────────────────────────────────────
_parsed_cfg=$(python3 - "${CONFIG_FILE}" "${SERVER_TYPE}" <<'PYEOF'
import sys

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        sys.stderr.write(
            "Error: TOML support is missing.\n"
            "  Python 3.11+ includes tomllib, or install tomli:\n"
            "    pip install tomli\n"
        )
        sys.exit(1)

with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)

server_type = sys.argv[2]
lg = cfg.get("logging", {})
print("NOM_LOG_DIR=" + repr(str(lg.get("log_dir", "logs"))))

# Camera capture settings apply to both server types: the standalone debug
# stream server below, and the API server's own camera (POST
# /api/stream/start reuses that same instance for still capture, recording,
# and streaming) — there is only one physical camera to configure.
s = cfg.get("stream", {})
print("NOM_STREAM_CAMERA="  + str(int(s.get("camera_index", 0))))
print("NOM_STREAM_WIDTH="   + str(int(s.get("width",        1280))))
print("NOM_STREAM_HEIGHT="  + str(int(s.get("height",       720))))
print("NOM_STREAM_FPS="     + str(int(s.get("fps",          30))))
print("NOM_STREAM_ENCODER=" + repr(str(s.get("encoder",     "h264"))))

if server_type == "stream":
    print("NOM_STREAM_HOST=" + repr(str(s.get("host", "0.0.0.0"))))
    print("NOM_STREAM_PORT=" + str(int(s.get("port",  8000))))
else:
    a = cfg.get("api", {})
    h = cfg.get("hat", {})
    md = cfg.get("media", {})
    au = cfg.get("audio", {})
    mq = cfg.get("mqtt", {})
    tl = cfg.get("telemetry", {})
    print("NOM_API_MODE="             + repr(str(a.get("api_mode",         "device"))))
    print("NOM_API_HOST="             + repr(str(a.get("host",             "0.0.0.0"))))
    print("NOM_API_PORT="             + str(int(a.get("port",              8443))))
    print("NOM_API_USE_SSL="          + str(bool(a.get("use_ssl",          True))).lower())
    print("NOM_API_CERT_DIR="         + repr(str(a.get("cert_dir",         ".certs"))))
    print("NOM_HAT_SOCKET="           + repr(str(h.get("socket_path",      ""))))
    print("NOMON_MEDIA_DIR="          + repr(str(md.get("dir",             "~/perceptua-nomon/media"))))
    # Only emitted when config.toml sets an explicit index — the default is
    # name-based auto-detection, and a blind numeric index can segfault
    # libportaudio (PortAudio indexes are not ALSA card numbers).
    if au.get("input_device_index") is not None:
        print("NOMON_AUDIO_INPUT_INDEX=" + str(int(au["input_device_index"])))
    print("NOMON_AUDIO_VOLUME="       + str(int(au.get("default_volume_pct",   80))))
    print("NOMON_AUDIO_MIC_GAIN="     + str(int(au.get("default_mic_gain_pct", 50))))
    # Wake-word vars are emitted only when config.toml sets a phrase, so an
    # empty [wakeword] section never clobbers values loaded from .env.device.
    wk = cfg.get("wakeword", {})
    wake_phrase = str(wk.get("phrase", "")).strip()
    if wake_phrase:
        print("NOMON_WAKE_PHRASE="            + repr(wake_phrase))
        print("NOMON_WAKE_PHRASE_VARIANTS="   + repr(str(wk.get("phrase_variants", ""))))
        print("NOMON_WAKE_FOLLOWUP_WINDOW_S=" + str(float(wk.get("followup_window_s", 8.0))))
        print("NOMON_WAKE_CHIME_VOLUME_PCT="  + str(int(wk.get("chime_volume_pct", 80))))
    print("NOMON_MQTT_BROKER="        + repr(str(mq.get("broker",          ""))))
    print("NOMON_MQTT_PORT="          + str(int(mq.get("port",             1883))))
    print("NOMON_MQTT_TOPIC="         + repr(str(mq.get("topic",           "nomon/telemetry"))))
    print("NOMON_MQTT_INTERVAL="      + str(float(mq.get("interval",       30.0))))
    print("NOMON_DEVICE_ID="          + repr(str(tl.get("device_id",       ""))))
PYEOF
)
eval "${_parsed_cfg}"

# ─── Resolve log directory ────────────────────────────────────────────────────
if [[ "${NOM_LOG_DIR}" != /* ]]; then
  NOM_LOG_DIR="${REPO_DIR}/${NOM_LOG_DIR}"
fi
mkdir -p "${NOM_LOG_DIR}"
LOG_FILE="${NOM_LOG_DIR}/${SERVER_TYPE}.log"

# ─── Expand ~ in NOMON_MEDIA_DIR ─────────────────────────────────────────────
if [[ -n "${NOMON_MEDIA_DIR:-}" ]]; then
  NOMON_MEDIA_DIR="${NOMON_MEDIA_DIR/#\~/$HOME}"
fi

# ─── Build server-specific launch snippet and display info ───────────────────
if [[ "${SERVER_TYPE}" == "stream" ]]; then
  export NOM_STREAM_HOST NOM_STREAM_PORT NOM_STREAM_CAMERA
  export NOM_STREAM_WIDTH NOM_STREAM_HEIGHT NOM_STREAM_FPS NOM_STREAM_ENCODER
  DISPLAY_URL="http://${NOM_STREAM_HOST}:${NOM_STREAM_PORT}"
  DISPLAY_EXTRA=""
  LAUNCH_PY="
import os
from nomothetic.streaming import StreamServer
StreamServer(
    host=os.environ['NOM_STREAM_HOST'],
    port=int(os.environ['NOM_STREAM_PORT']),
    camera_index=int(os.environ['NOM_STREAM_CAMERA']),
    width=int(os.environ['NOM_STREAM_WIDTH']),
    height=int(os.environ['NOM_STREAM_HEIGHT']),
    fps=int(os.environ['NOM_STREAM_FPS']),
    encoder=os.environ['NOM_STREAM_ENCODER'],
).start()
"
else
  if [[ -n "${NOM_HAT_SOCKET-}" ]]; then
    NOMON_HAT_SOCKET_PATH="${NOM_HAT_SOCKET}"
  fi
  export NOM_API_MODE NOM_API_HOST NOM_API_PORT NOM_API_USE_SSL NOM_API_CERT_DIR NOMON_HAT_SOCKET_PATH
  export NOMON_MEDIA_DIR NOMON_AUDIO_INPUT_INDEX NOMON_AUDIO_VOLUME NOMON_AUDIO_MIC_GAIN
  export NOM_STREAM_CAMERA NOM_STREAM_WIDTH NOM_STREAM_HEIGHT NOM_STREAM_FPS NOM_STREAM_ENCODER
  export NOMON_MQTT_BROKER NOMON_MQTT_PORT NOMON_MQTT_TOPIC NOMON_MQTT_INTERVAL
  export NOMON_DEVICE_ID
  # Exporting unset names is a no-op — these only propagate when config.toml
  # emitted them (see the wakeword block above) or .env.device set them.
  export NOMON_WAKE_PHRASE NOMON_WAKE_PHRASE_VARIANTS
  export NOMON_WAKE_FOLLOWUP_WINDOW_S NOMON_WAKE_CHIME_VOLUME_PCT
  export NOMON_API_MODE="${NOMON_API_MODE:-${NOM_API_MODE}}"

  if [[ "${NOMON_API_MODE}" == "central" ]]; then
    if [[ -z "${NOMON_JWT_SECRET:-}" || ${#NOMON_JWT_SECRET} -lt 32 ]]; then
      echo "Error: central mode requires NOMON_JWT_SECRET (min 32 chars)." >&2
      echo "Set it in ${REPO_DIR}/.env.central or export it before starting the API." >&2
      exit 1
    fi
  fi

  SCHEME="$([[ "${NOM_API_USE_SSL}" == "true" ]] && echo "https" || echo "http")"
  DISPLAY_URL="${SCHEME}://${NOM_API_HOST}:${NOM_API_PORT}"
  DISPLAY_EXTRA="  Docs: ${DISPLAY_URL}/docs"$'\n'
  LAUNCH_PY="
import os
from nomothetic.api import APIServer
APIServer(
    host=os.environ['NOM_API_HOST'],
    port=int(os.environ['NOM_API_PORT']),
    use_ssl=(os.environ['NOM_API_USE_SSL'] == 'true'),
    cert_dir=os.environ['NOM_API_CERT_DIR'] or None,
).run()
"
fi

# ─── Foreground mode ─────────────────────────────────────────────────────────
if [[ "${FOREGROUND}" == "true" ]]; then
  echo "Starting ${SERVER_TYPE} server in the foreground (Ctrl-C to stop)..."
  echo "  URL:  ${DISPLAY_URL}"
  exec python3 -c "${LAUNCH_PY}"
fi

# ─── Check if already running ────────────────────────────────────────────────
if [[ -f "${PID_FILE}" ]]; then
  OLD_PID="$(cat "${PID_FILE}")"
  if kill -0 "${OLD_PID}" 2>/dev/null; then
    echo "${SERVER_TYPE} server is already running (PID ${OLD_PID})."
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

# ─── Launch server in background ─────────────────────────────────────────────
nohup python3 -c "${LAUNCH_PY}" >> "${LOG_FILE}" 2>&1 &

SERVER_PID=$!
echo "${SERVER_PID}" > "${PID_FILE}"

echo "${SERVER_TYPE} server started."
echo "  PID:  ${SERVER_PID}  (${PID_FILE})"
echo "  URL:  ${DISPLAY_URL}"
printf '%s' "${DISPLAY_EXTRA}"
echo "  Logs: ${LOG_FILE}"
echo "  Stop: ./scripts/stop.sh ${SERVER_TYPE}"
