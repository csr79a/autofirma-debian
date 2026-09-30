#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP="${SCRIPT_DIR}/autofirma_gui_qt.py"

if [[ "${EUID}" -eq 0 ]]; then
    echo "ERROR: no ejecutes este lanzador como root."
    echo "Inícialo como usuario normal para que PyQt6 y PolicyKit funcionen en tu sesión gráfica."
    exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "ERROR: este instalador necesita Python 3.10 o superior."
    python3 --version
    exit 1
fi

# pkexec y polkitd son necesarios para las operaciones con
# privilegios de administrador, incluida la propia instalación de
# python3-pyqt6 más abajo. Se comprueba primero por eso.
if ! command -v pkexec >/dev/null 2>&1 || ! command -v polkitd >/dev/null 2>&1; then
    echo "Faltan pkexec y/o polkitd. Intentando instalar los paquetes actuales…"
    if command -v apt >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
        if sudo apt install -y pkexec polkitd; then
            echo "pkexec y polkitd instalados correctamente."
            echo "Puede que haga falta cerrar sesión o reiniciar para que el"
            echo "servicio polkitd arranque. Si el siguiente paso falla,"
            echo "reinicia sesión y vuelve a ejecutar este script."
        else
            echo "ERROR: no se pudieron instalar pkexec y polkitd automáticamente."
            echo "Instálalos manualmente con: sudo apt install pkexec polkitd"
            exit 1
        fi
    else
        echo "ERROR: no se encuentra apt o sudo para instalar la dependencia."
        echo "Instálalos manualmente con: sudo apt install pkexec polkitd"
        exit 1
    fi
fi

if command -v systemctl >/dev/null 2>&1; then
    if ! systemctl is-active --quiet polkit 2>/dev/null && \
       ! systemctl is-active --quiet polkitd 2>/dev/null; then
        echo "AVISO: polkit/polkitd no está activo. Si la app falla al pedir"
        echo "privilegios, comprueba la sesión gráfica y el agente de autenticación de PolicyKit."
    fi
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
