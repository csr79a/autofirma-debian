#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP="${SCRIPT_DIR}/autofirma_gui_qt.py"

command -v python3 >/dev/null 2>&1 || {
    echo "ERROR: Python 3 no está instalado."
    exit 1
}

# pkexec (paquete policykit-1) es necesario para las operaciones con
# privilegios de administrador, incluida la propia instalación de
# python3-pyqt6 más abajo. Se comprueba primero por eso.
if ! command -v pkexec >/dev/null 2>&1; then
    echo "Falta pkexec (paquete policykit-1). Intentando instalarlo…"
    if command -v apt >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
        if sudo apt install -y policykit-1; then
            echo "policykit-1 instalado correctamente."
            echo "Puede que haga falta cerrar sesión o reiniciar para que el"
            echo "servicio polkitd arranque. Si el siguiente paso falla,"
            echo "reinicia sesión y vuelve a ejecutar este script."
        else
            echo "ERROR: no se pudo instalar policykit-1 automáticamente."
            echo "Instálalo manualmente con: sudo apt install policykit-1"
            exit 1
        fi
    else
        echo "ERROR: no se encuentra apt o sudo para instalar la dependencia."
        echo "Instálalo manualmente con: sudo apt install policykit-1"
        exit 1
    fi
fi

if ! systemctl is-active --quiet polkit 2>/dev/null; then
    echo "AVISO: el servicio polkit no está activo. Si la app falla al pedir"
    echo "privilegios, cierra sesión (o reinicia) y vuelve a intentarlo."
fi

if ! python3 -c "import PyQt6" >/dev/null 2>&1; then
    echo "Falta el módulo PyQt6 (paquete python3-pyqt6). Intentando instalarlo…"
    if command -v apt >/dev/null 2>&1 && command -v pkexec >/dev/null 2>&1; then
        if pkexec apt install -y python3-pyqt6; then
            echo "python3-pyqt6 instalado correctamente."
        else
            echo "ERROR: no se pudo instalar python3-pyqt6 automáticamente."
            echo "Instálalo manualmente con: sudo apt install python3-pyqt6"
            exit 1
        fi
    else
        echo "ERROR: no se encuentra apt o pkexec para instalar la dependencia."
        echo "Instálalo manualmente con: sudo apt install python3-pyqt6"
        exit 1
    fi
fi

exec python3 "$APP" "$@"
