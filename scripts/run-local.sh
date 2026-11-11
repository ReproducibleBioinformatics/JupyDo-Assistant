#!/usr/bin/env bash
# Runs the JupyDo Assistant alone, without JupyDo and without login, on this computer:
# the same service and model that JupyDo uses. Open the address it prints.
#
# Needs: Docker, curl, sha256sum (or shasum on macOS). Run it from a clone of this
# repository. The first time it downloads the model (1.1 GB, from this repository's
# GitHub release, checked against its SHA-256) and the llama.cpp server image (from
# ghcr.io); then it starts in a few seconds. Ctrl-C stops everything.
#
# Usage: scripts/run-local.sh [--port 10108]
# Environment: ASSISTANT_HOME  where the model is kept (default ~/.jupydo-assistant)
set -euo pipefail

PORT=10108
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${ASSISTANT_HOME:-$HOME/.jupydo-assistant}"
RELEASE="https://github.com/ReproducibleBioinformatics/JupyDo-Assistant/releases/download/model-qwen2.5-1.5b-instruct-q4_k_m"
MODEL="qwen2.5-1.5b-instruct-q4_k_m.gguf"
MODEL_SHA256="6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
LLM_IMAGE="$(sed -n 's/^LLM_IMAGE = "\(.*\)"/\1/p' "$REPO/assistant/llm.py")"
SERVICE_IMAGE="jupydo-assistant-local"
NETWORK="jupydo-assistant-local"
CATALOG="$REPO/catalog.json"

die() { echo "ERROR: $*" >&2; exit 1; }
command -v docker >/dev/null || die "Docker is not installed (https://docs.docker.com/get-docker/)"
docker info >/dev/null 2>&1 || die "Docker is not running, or this user cannot use it"
command -v curl >/dev/null || die "curl is not installed"
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
[ -n "$LLM_IMAGE" ] || die "cannot read LLM_IMAGE from assistant/llm.py"
[ -f "$CATALOG" ] || die "catalog.json is missing in $REPO (git pull, or build it with scripts/make-catalog.py)"

# 1. the model, once
mkdir -p "$HOME_DIR"
if [ ! -f "$HOME_DIR/$MODEL" ] || [ "$(sha256 "$HOME_DIR/$MODEL")" != "$MODEL_SHA256" ]; then
    echo "Downloading the model (1.1 GB) into $HOME_DIR ..."
    curl -fL --progress-bar -o "$HOME_DIR/$MODEL.part" "$RELEASE/$MODEL"
    [ "$(sha256 "$HOME_DIR/$MODEL.part")" = "$MODEL_SHA256" ] || die "the downloaded model is damaged (checksum): run again"
    mv "$HOME_DIR/$MODEL.part" "$HOME_DIR/$MODEL"
    curl -fsSL -o "$HOME_DIR/LICENSE" "$RELEASE/LICENSE"
fi

# 2. the images: llama.cpp server (ghcr.io) and the service (python 3.9, like the JupyDo hub)
docker image inspect "$LLM_IMAGE" >/dev/null 2>&1 || { echo "Downloading the llama.cpp server image ..."; docker pull -q "$LLM_IMAGE"; }
docker image inspect "$SERVICE_IMAGE" >/dev/null 2>&1 || {
    echo "Preparing the service image ..."
    printf 'FROM python:3.9-slim\nRUN pip install --no-cache-dir -q "tornado>=6.4" "docker==7.1.0"\n' \
        | docker build -q -t "$SERVICE_IMAGE" - >/dev/null
}

# 3. start: the service starts the model by itself when someone opens the chat
docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
cleanup() {
    docker rm -f jupydo-assistant-local jupydo-assistant-llm >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup
echo
echo "  JupyDo Assistant: http://localhost:$PORT/services/assistant/"
echo "  (Ctrl-C to stop)"
echo
docker run --rm --name jupydo-assistant-local --network "$NETWORK" -p "127.0.0.1:$PORT:10108" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$REPO:/app:ro" \
    -e ASSISTANT_DEV=1 -e ASSISTANT_DEV_HOST=0.0.0.0 -e ASSISTANT_DEV_PORT=10108 \
    -e ASSISTANT_CATALOG=/app/catalog.json \
    -e ASSISTANT_LLM_IMAGE="$LLM_IMAGE" \
    -e ASSISTANT_MODEL_DIR="$HOME_DIR" -e ASSISTANT_MODEL_FILE="$MODEL" \
    -e ASSISTANT_LLM_NETWORK="$NETWORK" \
    "$SERVICE_IMAGE" python3 /app/assistant/service.py
