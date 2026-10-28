#!/usr/bin/env bash
# start-linux.sh - jalankan Snapgent bridge di Linux.
# Menyiapkan venv + library websockets (sekali saja), menulis config.json
# (sekali saja), lalu menjalankan bridge. Biarkan terminal ini tetap terbuka
# selama memakai Snapgent; Ctrl+C untuk berhenti.
set -e
cd "$(dirname "$0")"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "ERROR: python3 tidak ditemukan. Pasang Python 3.9+ dulu."
  exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "ERROR: butuh Python 3.9+. Versi terpasang: $("$PY" --version 2>&1)"
  exit 1
}

if [ ! -x .venv/bin/python ]; then
  echo "[setup] membuat .venv ..."
  "$PY" -m venv .venv
fi
if ! .venv/bin/python -c "import websockets" >/dev/null 2>&1; then
  echo "[setup] memasang library websockets ..."
  .venv/bin/pip install -q websockets
fi

if [ ! -f config.json ]; then
  echo "[setup] menulis config.json (APPDATA diarahkan ke \$HOME/.config) ..."
  .venv/bin/python - <<EOF
import json, os
cfg = {"mcpServers": {"vscode": {
    "command": "python3",
    "args": [os.path.join(os.getcwd(), "vscode_mcp.py")],
    "env": {"APPDATA": os.path.join(os.path.expanduser("~"), ".config")},
}}}
with open("config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
print("[setup] config.json:", json.dumps(cfg))
EOF
fi

echo
echo "  Snapgent bridge (Linux) - biarkan terminal ini terbuka."
echo "  Pastikan VS Code sudah membuka folder proyekmu."
echo
exec .venv/bin/python bridge.py
