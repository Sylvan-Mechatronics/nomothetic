#!/usr/bin/env bash
# fetch_stt_model.sh — Download the Vosk STT model for voice-command transcription.
#
# POST /api/ai/transcribe recognises speech on-device with a local Vosk model
# (see docs/adr/020-on-device-stt-in-nomothetic.md). This script downloads and
# unpacks the small English model to the directory the API reads it from.
#
# Usage:
#   ./scripts/fetch_stt_model.sh [<dest-dir>]
#
# Arguments:
#   dest-dir   Directory to unpack the model into. Defaults to the parent of
#              NOMON_STT_MODEL_PATH if set, else /var/lib/nomon/stt.
#
# The model name follows NOMON_STT_MODEL_PATH's basename when it looks like a
# Vosk model directory (vosk-model-*), so a config pointing at a different
# official model fetches that one; otherwise the small English default is
# used. Official zips unpack to <dest-dir>/<model-name>.
#
# Requires: curl, unzip. Uses sudo only when the destination is not writable
# by the current user, and makes the tree readable by the nomon service group.
set -euo pipefail

DEFAULT_MODEL_NAME="vosk-model-small-en-us-0.15"
if [[ -n "${NOMON_STT_MODEL_PATH:-}" ]] \
        && [[ "$(basename "${NOMON_STT_MODEL_PATH}")" == vosk-model-* ]]; then
    MODEL_NAME="$(basename "${NOMON_STT_MODEL_PATH}")"
else
    MODEL_NAME="${DEFAULT_MODEL_NAME}"
fi
MODEL_URL="https://alphacephei.com/vosk/models/${MODEL_NAME}.zip"
SERVICE_GROUP="${NOMON_SERVICE_GROUP:-nomon}"

default_dest() {
    if [[ -n "${NOMON_STT_MODEL_PATH:-}" ]]; then
        dirname "${NOMON_STT_MODEL_PATH}"
    else
        echo "/var/lib/nomon/stt"
    fi
}

DEST_DIR="${1:-$(default_dest)}"
MODEL_DIR="${DEST_DIR}/${MODEL_NAME}"

for tool in curl unzip; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "error: $tool is required (sudo apt install -y $tool)" >&2
        exit 1
    fi
done

if [[ -d "$MODEL_DIR" ]]; then
    echo "STT model already installed at ${MODEL_DIR} — nothing to do."
    exit 0
fi

# Escalate only if we cannot create/write the destination ourselves.
SUDO=""
if ! mkdir -p "$DEST_DIR" 2>/dev/null || [[ ! -w "$DEST_DIR" ]]; then
    SUDO="sudo"
    $SUDO mkdir -p "$DEST_DIR"
fi

TMP_ZIP="$(mktemp --suffix=.zip)"
trap 'rm -f "$TMP_ZIP"' EXIT

echo "Downloading ${MODEL_URL} ..."
curl -fL --retry 3 -o "$TMP_ZIP" "$MODEL_URL"

echo "Unpacking to ${MODEL_DIR} ..."
$SUDO unzip -q "$TMP_ZIP" -d "$DEST_DIR"

if getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    $SUDO chgrp -R "$SERVICE_GROUP" "$MODEL_DIR"
    $SUDO chmod -R g+rX "$MODEL_DIR"
fi

echo "Done. The API reads the model from NOMON_STT_MODEL_PATH"
echo "(default ${MODEL_DIR}); restart nomothetic-api to pick it up."
