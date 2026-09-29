#!/usr/bin/env bash
set -Eeuo pipefail

# Lanzador de compatibilidad:
# la interfaz principal del proyecto es ahora la versión PyQt6.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/instalar_autofirma_qt.sh" "$@"
