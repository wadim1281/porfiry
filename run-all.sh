#!/usr/bin/env bash
set -euo pipefail

# Settings (can be overridden via env)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="${REPO:-$SCRIPT_DIR}"
VENV="${VENV:-$REPO/.venv}"
PY_BIN="${PY_BIN:-python3}"
NO_INSTALL="${NO_INSTALL:-0}"
PORT_API="${PORT_API:-8000}"
PORT_OCR="${PORT_OCR:-8001}"
PORT_UI="${PORT_UI:-8501}"

# Example model endpoints (uncomment/override if needed)
# export OLLAMA_HOST="http://127.0.0.1:11434"
# export OLLAMA_URL="http://127.0.0.1:11434"

cd "$REPO"

# venv + dependencies (first run)
if [[ ! -d "$VENV" && "$NO_INSTALL" != "1" ]]; then
  echo "[*] Create venv: $VENV"
  "$PY_BIN" -m venv "$VENV"
  "$VENV/bin/pip" install -U pip
  "$VENV/bin/pip" install \
    fastapi uvicorn \
    streamlit streamlit-sortables streamlit-markdown streamlit-extras streamlit-draggable-list \
    requests httpx tinydb psutil ollama pillow pydantic
fi

UVICORN="$VENV/bin/uvicorn"
STREAMLIT="$VENV/bin/streamlit"

# Start services bound to localhost only
echo "[*] Start backend_en (127.0.0.1:$PORT_API) ..."
"$UVICORN" backend:app --host 127.0.0.1 --port "$PORT_API" --reload & BE_PID=$!

echo "[*] Start ocr (127.0.0.1:$PORT_OCR) ..."
"$UVICORN" ocr:app --host 127.0.0.1 --port "$PORT_OCR" --reload & OCR_PID=$!

cleanup() {
  echo
  echo "[*] Stop services..."
  kill "$BE_PID" "$OCR_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# UI in foreground
echo "[*] Start UI (http://localhost:$PORT_UI)"
exec "$STREAMLIT" run "$REPO/porfiry.py" --server.port "$PORT_UI" 
