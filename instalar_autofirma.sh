#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP="${SCRIPT_DIR}/autofirma_gui.py"

command -v python3 >/dev/null 2>&1 || {
    echo "ERROR: Python 3 no está instalado."
    exit 1
}

exec python3 "$APP" "$@"
