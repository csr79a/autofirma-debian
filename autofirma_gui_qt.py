#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instalador de AutoFirma — Debian (PyQt6)

Port funcional (no demo) de autofirma_gui.py (Tkinter) a PyQt6. Toda la
lógica de negocio (subprocess, pkexec, NSS, descarga oficial con
verificación SHA-256, dependencias JRE, confianza del certificado local
en navegadores...) está portada 1:1; solo cambia la capa de interfaz
(Tkinter/ttk -> PyQt6) y el mecanismo de concurrencia
(threading+queue+polling -> QThread + señales).

Dependencias del sistema: las mismas que la versión Tkinter (pkexec,
policykit-1, libnss3-tools, openssl, curl, Java) más el paquete Debian
`python3-pyqt6` en vez de `python3-tk`. El lanzador
`instalar_autofirma_qt.sh` comprueba e instala esta dependencia igual
que el original hace con python3-tk.
"""

import hashlib
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

OFFICIAL_PAGE = "https://firmaelectronica.gob.es/descargas"
# Incidencia conocida: el portal oficial puede presentar temporalmente un
# certificado TLS caducado. Como respaldo de emergencia, usamos SOLO la URL
# oficial exacta y una huella SHA-256 fijada para AutoFirma 1.9 Debian.
# Si la página oficial vuelve a funcionar por TLS, esta ruta de respaldo no se usa.
OFFICIAL_DEBIAN_URL = (
    "https://firmaelectronica.gob.es/content/dam/firmaelectronica/"
    "descargas-software/autofirma19/Autofirma_Linux_Debian.zip"
)
OFFICIAL_DEBIAN_VERSION = "1.9"
OFFICIAL_DEBIAN_SHA256 = "c29c251f2ee9f00dfc87f9582677dbd436a83565986ab0417ff065ceae716798"
OFFICIAL_DEBIAN_SIZE = 67295518
NSS_DIR = Path.home() / ".pki" / "nssdb"
# Certificado de confianza que AutoFirma genera para su comunicación SSL
# local (127.0.0.1). El paquete Debian no lo registra de forma fiable en
# todos los navegadores (SAF_04 en Brave, "Cargando" infinito en Firefox).
AUTOFIRMA_ROOT_CERT = Path("/usr/lib/Autofirma/Autofirma_ROOT.cer")
AUTOFIRMA_ROOT_NICKNAME = "AutoFirma ROOT"


# =======================================================================
# Lógica de negocio — sin ninguna dependencia de Qt, para poder llamarla
# desde un QThread sin tocar widgets desde fuera del hilo principal.
# Los métodos que tardan (instalación, descarga...) aceptan un callback
# `log(text)` opcional para reportar progreso; el resto son puros.
# =======================================================================

class AutoFirmaCore:
    JRE_CANDIDATES = ["openjdk-17-jre", "openjdk-21-jre"]

    # ---------- Helpers de proceso ----------

    def _run(self, args, *, sudo=False, input_text=None, check=True):
        if sudo:
            args = ["pkexec", "--disable-internal-agent"] + list(args)
        return subprocess.run(
            args,
            input=(input_text.encode() if input_text is not None else None),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
            text=False,
        )

    def _command_exists(self, command):
        return shutil.which(command) is not None

    def _require_pkexec(self):
        if not self._command_exists("pkexec"):
            raise RuntimeError(
                "No se encuentra pkexec. Instala policykit-1 o ejecuta la "
                "aplicación desde un entorno Debian con PolicyKit disponible."
            )

    # ---------- Estado ----------

    def get_installed_version(self):
        try:
            p = subprocess.run(
                ["dpkg-query", "-W", "-f=${Version}", "autofirma"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except Exception:
            pass
        return ""

    # ---------- Dependencias de AutoFirma ----------

    def _apt_has_candidate(self, package):
        p = subprocess.run(
            ["apt-cache", "policy", package],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        if p.returncode != 0:
            detail = p.stderr.strip() or "sin detalles."
            raise RuntimeError(
                f"apt-cache no pudo consultar «{package}» (código {p.returncode}).

{detail}"
            )
        for line in p.stdout.splitlines():
            line = line.strip()
            if line.startswith("Candidato:") or line.startswith("Candidate:"):
                valor = line.split(":", 1)[1].strip()
                return valor not in ("(ninguno)", "(none)", "")
        return False

    def _pick_jre_package(self):
        for pkg in self.JRE_CANDIDATES:
            if self._apt_has_candidate(pkg):
                return pkg
        raise RuntimeError(
            "Ninguna de las JRE candidatas (" + ", ".join(self.JRE_CANDIDATES) +
            ") tiene versión disponible en los repositorios APT configurados.\n\n"
            "Comprueba 'apt-cache policy <paquete>' y tus sources.list."
        )

    def ensure_runtime_dependencies(self, log=lambda t: None):
        """Instala Java/NSS Tools/curl si faltan.

        Devuelve True si en esta llamada se ha instalado una JRE que antes
        no estaba presente (relevante para quien lo invoca: si AutoFirma ya
        estaba instalado y la JRE llega DESPUÉS, hace falta reinstalar el
        paquete para que su postinst regenere el almacén SSL del WebSocket
        local — ver SAF_45).
        """
        self._require_pkexec()

        necesita_java = not self._command_exists("java")
        if not necesita_java:
            p = subprocess.run(["java", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            text = (p.stdout + p.stderr).decode(errors="replace")
            necesita_java = "OpenJDK" not in text and "Java" not in text

        necesita_nss = not self._command_exists("certutil")
        necesita_curl = not self._command_exists("curl")

        if not (necesita_java or necesita_nss or necesita_curl):
            log("Java, NSS Tools y curl ya están disponibles.")
            return False

        log("Actualizando índice de APT…")
        p = self._run(["apt-get", "update"], sudo=True, check=False)
        if p.returncode != 0:
            detail = p.stderr.decode(errors="replace").strip()
            raise RuntimeError("No se pudo actualizar APT para instalar las dependencias." + (f"\n\n{detail}" if detail else ""))

        missing = []
        if necesita_java:
            missing.append(self._pick_jre_package())
        if necesita_nss:
            missing.append("libnss3-tools")
        if necesita_curl:
            missing.append("curl")
        missing = list(dict.fromkeys(missing))

        log("Instalando dependencias necesarias: " + ", ".join(missing))
        p = self._run(["apt-get", "install", "-y"] + missing, sudo=True, check=False)
        if p.returncode != 0:
            detail = p.stderr.decode(errors="replace").strip()
            raise RuntimeError("No se pudieron instalar las dependencias de AutoFirma." + (f"\n\n{detail}" if detail else ""))

        if not self._command_exists("java"):
            raise RuntimeError("Java sigue sin estar disponible después de la instalación.")
        if not self._command_exists("certutil"):
            raise RuntimeError("NSS Tools sigue sin estar disponible después de la instalación.")
        log("Dependencias instaladas correctamente.")
        p = subprocess.run(["java", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        version_text = (p.stdout + p.stderr).decode(errors="replace").strip().splitlines()
        if version_text:
            log("Java: " + version_text[0])

        return necesita_java

    def verify_autofirma_installation(self, log=lambda t: None):
        if not self._command_exists("java"):
            raise RuntimeError("AutoFirma está instalada pero Java no está disponible.")
        if not self._command_exists("certutil"):
            raise RuntimeError("AutoFirma está instalada pero faltan NSS Tools (certutil).")
        autofirma = shutil.which("AutoFirma") or shutil.which("autofirma")
        if not autofirma:
            raise RuntimeError("El paquete se instaló, pero no existe el ejecutable /usr/bin/AutoFirma.")
        if not Path("/usr/lib/Autofirma").exists():
            raise RuntimeError("El paquete se instaló, pero falta /usr/lib/Autofirma.")
        try:
            p = subprocess.run([autofirma, "-help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "AutoFirma no respondió dentro de 20 segundos durante la verificación. "
                "Cierra cualquier instancia de AutoFirma y vuelve a intentarlo."
            ) from exc
        output = (p.stdout + p.stderr).decode(errors="replace")
        if p.returncode != 0:
            raise RuntimeError("AutoFirma está instalada pero no puede ejecutarse con la Java disponible.\n\n" + output[-2000:])
        log("AutoFirma responde correctamente con Java y NSS Tools.")
        return autofirma

    # ---------- HTTPS / descarga oficial ----------

    def _system_time_ok(self):
        now = datetime.now()
        if now.year < 2020:
            return False, f"La fecha del sistema parece incorrecta: {now:%Y-%m-%d %H:%M:%S}"
        return True, f"Fecha del sistema: {now:%Y-%m-%d %H:%M:%S}"

    def _ca_bundle(self):
        candidates = [
            os.environ.get("SSL_CERT_FILE"),
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/ssl/cert.pem",
        ]
        for item in candidates:
            if item and Path(item).is_file():
                return item
        return None

    def _https_context(self):
        cafile = self._ca_bundle()
        if not cafile:
            raise RuntimeError(
                "No se encuentra el almacén de certificados CA de Debian "
                "(/etc/ssl/certs/ca-certificates.crt)."
            )
        return ssl.create_default_context(cafile=cafile)

    def _check_official_https(self, log=lambda t: None):
        ok_time, time_msg = self._system_time_ok()
        log(time_msg)
        if not ok_time:
            raise RuntimeError(time_msg + "\n\nCorrige la fecha/hora del sistema y vuelve a intentarlo.")

        cafile = self._ca_bundle()
        if not cafile:
            log("No se encuentra el bundle CA de Debian.")
            log("Intentando reparar ca-certificates…")
            self._repair_ca_certificates(log)
            cafile = self._ca_bundle()

        if not cafile:
            raise RuntimeError("No se pudo localizar el almacén CA después de reparar ca-certificates.")

        log(f"Usando certificados CA del sistema: {cafile}")
        context = self._https_context()
        request = urllib.request.Request(
            OFFICIAL_PAGE, headers={"User-Agent": "autofirma-debian-installer/1.1"},
        )

        try:
            with urllib.request.urlopen(request, context=context, timeout=30) as response:
                data = response.read()
        except ssl.SSLCertVerificationError as exc:
            # Nunca hacemos fallback a verify=False.
            detail = str(exc)
            if "certificate has expired" in detail.lower():
                raise RuntimeError(
                    "El servidor oficial de AutoFirma presenta un certificado HTTPS caducado.\n\n"
                    "La fecha/hora de este equipo es correcta y los certificados CA del sistema "
                    "están disponibles, por lo que no se modificará nada ni se realizará una "
                    "descarga insegura.\n\n"
                    "Cuando el portal oficial renueve su certificado, vuelve a ejecutar el instalador."
                ) from exc
            raise RuntimeError(
                "No se pudo verificar el certificado HTTPS de la página oficial.\n\n"
                f"Detalle: {exc}\n\n"
                "No se realizará ninguna descarga insegura. "
                "Comprueba la fecha/hora del sistema y los certificados CA."
            ) from exc
        except ssl.SSLError as exc:
            raise RuntimeError(f"Falló la conexión TLS con la página oficial.\n\nDetalle: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"No se pudo acceder a la página oficial:\n{exc}") from exc

        return data.decode("utf-8", errors="replace")

    def _repair_ca_certificates(self, log=lambda t: None):
        self._require_pkexec()
        p = self._run(["apt-get", "update"], sudo=True, check=False)
        if p.returncode != 0:
            detail = p.stderr.decode(errors="replace").strip()
            raise RuntimeError("No se pudo actualizar el índice de paquetes para reparar ca-certificates." + (f"\n\n{detail}" if detail else ""))

        p = self._run(["apt-get", "install", "--reinstall", "-y", "ca-certificates"], sudo=True, check=False)
        if p.returncode != 0:
            detail = p.stderr.decode(errors="replace").strip()
            raise RuntimeError("No se pudo reinstalar ca-certificates." + (f"\n\n{detail}" if detail else ""))

        p = self._run(["update-ca-certificates"], sudo=True, check=False)
        if p.returncode != 0:
            detail = p.stderr.decode(errors="replace").strip()
            raise RuntimeError("No se pudo reconstruir el almacén CA." + (f"\n\n{detail}" if detail else ""))

        log("ca-certificates reparado/actualizado correctamente.")

    def get_official_deb(self, log=lambda t: None):
        log("Consultando la página oficial de AutoFirma…")
        try:
            html = self._check_official_https(log)
        except RuntimeError as exc:
            message = str(exc).lower()
            if "certificado https caducado" not in message and "certificate has expired" not in message:
                raise
            log(
                "El portal oficial presenta un certificado HTTPS caducado. "
                "Se usará la descarga oficial fijada de AutoFirma 1.9 y se verificará su SHA-256."
            )
            return OFFICIAL_DEBIAN_URL, "zip", OFFICIAL_DEBIAN_SHA256, OFFICIAL_DEBIAN_SIZE

        hrefs = re.findall(r'href\s*=\s*["\']([^"\']+)["\']', html, re.I)
        candidates = []
        for href in hrefs:
            absolute = urllib.parse.urljoin(OFFICIAL_PAGE, href)
            parsed = urllib.parse.urlparse(absolute)
            host = parsed.hostname or ""
            low = absolute.lower()
            if not (host == "firmaelectronica.gob.es" or host.endswith(".firmaelectronica.gob.es")):
                continue
            if "autofirma" in low and "debian" in low and low.endswith(".zip"):
                candidates.append(absolute)

        if not candidates:
            raise RuntimeError("La página oficial respondió correctamente, pero no se encontró el paquete Debian de AutoFirma.")

        return candidates[0], "zip", None, None

    def download_deb(self, log=lambda t: None):
        url, kind, expected_sha256, expected_size = self.get_official_deb(log)
        log(f"Paquete oficial: {url}")

        temp_dir = Path(tempfile.mkdtemp(prefix="autofirma-"))
        try:
            filename = Path(urllib.parse.urlparse(url).path).name or "autofirma-package"
            target = temp_dir / filename

            if expected_sha256:
                if not self._command_exists("curl"):
                    raise RuntimeError("Se necesita curl para la descarga de recuperación segura. Instálalo con: sudo apt install curl")
                log(
                    "AVISO: esta descarga se realiza con --insecure (verificación TLS de "
                    "cadena/hostname/caducidad DESACTIVADA para esta URL exacta), porque "
                    "el certificado del portal está caducado. La integridad del archivo se "
                    "garantiza después comparando tamaño exacto y SHA-256 contra los valores "
                    "oficiales fijados en el código (no por la conexión TLS)."
                )
                log("Descargando la versión oficial fijada de AutoFirma 1.9…")
                cmd = [
                    "curl", "--location", "--fail", "--silent", "--show-error",
                    "--insecure", "--output", str(target), url,
                ]
                p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if p.returncode != 0:
                    raise RuntimeError("No se pudo descargar el paquete oficial de AutoFirma.\n\n" f"curl: {p.stderr.strip()}")

                size = target.stat().st_size if target.exists() else 0
                if size != expected_size:
                    raise RuntimeError(
                        "La descarga oficial no coincide con el tamaño esperado. "
                        "Se ha rechazado y no se instalará.\n\n"
                        f"Esperado: {expected_size} bytes\nRecibido: {size} bytes"
                    )

                digest = hashlib.sha256(target.read_bytes()).hexdigest().lower()
                if digest != expected_sha256.lower():
                    raise RuntimeError(
                        "La descarga oficial no coincide con la huella SHA-256 esperada. "
                        "Se ha rechazado y no se instalará.\n\n"
                        f"Esperada: {expected_sha256}\nRecibida:  {digest}"
                    )
                log("SHA-256 verificado correctamente.")
            else:
                context = self._https_context()
                request = urllib.request.Request(url, headers={"User-Agent": "autofirma-debian-installer/1.2"})
                with urllib.request.urlopen(request, context=context, timeout=120) as response, target.open("wb") as output:
                    shutil.copyfileobj(response, output)

            if not target.is_file() or target.stat().st_size == 0:
                raise RuntimeError("La descarga oficial está vacía.")

            extract = temp_dir / "extract"
            extract.mkdir()
            with zipfile.ZipFile(target) as zf:
                bad = zf.testzip()
                if bad:
                    raise RuntimeError(f"El ZIP oficial está dañado (entrada: {bad}).")
                zf.extractall(extract)

            debs = list(extract.rglob("*.deb"))
            if not debs:
                raise RuntimeError("La descarga oficial no contiene ningún paquete .deb.")
            if len(debs) > 1:
                debs.sort(key=lambda x: x.stat().st_size, reverse=True)
            return debs[0], temp_dir
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    # ---------- Instalar / actualizar ----------

    def install_or_update(self, log=lambda t: None, status=lambda n, v: None):
        """Devuelve (mensaje_info, autofirma_cmd) o (mensaje_info, None) si no
        había nada que actualizar."""
        temp_dir = None
        try:
            self._require_pkexec()
            log("Comprobando AutoFirma…")

            installed = self.get_installed_version()
            java_recien_instalado = self.ensure_runtime_dependencies(log)
            if installed:
                log(f"Versión instalada: {installed}")
            else:
                log("AutoFirma no está instalada.")

            deb, temp_dir = self.download_deb(log)

            p = subprocess.run(
                ["dpkg-deb", "-f", str(deb), "Version"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
            )
            available = p.stdout.strip()
            status("version_oficial", available)
            log(f"Versión oficial disponible: {available}")

            reinstalar_por_java_tardio = False
            if installed:
                cmp = subprocess.run(["dpkg", "--compare-versions", available, "gt", installed]).returncode
                eq = subprocess.run(["dpkg", "--compare-versions", available, "eq", installed]).returncode

                if eq == 0:
                    if java_recien_instalado:
                        log(
                            f"AutoFirma ya está en su versión más nueva ({installed}), pero Java "
                            "se acaba de instalar en esta misma operación. AutoFirma se instaló "
                            "originalmente sin una JRE disponible, lo que puede dejar roto el "
                            "almacén SSL del WebSocket local (error SAF_45 al firmar desde el "
                            "navegador). Se reinstalará el paquete para regenerarlo."
                        )
                        reinstalar_por_java_tardio = True
                    else:
                        log(f"AutoFirma ya está en su versión más nueva ({installed}). No hay nada que actualizar.")
                        return None, None
                elif cmp != 0:
                    log(f"La versión instalada ({installed}) es superior a la descargada ({available}). No se modifica AutoFirma.")
                    return None, None
                else:
                    log(f"Actualizando AutoFirma a {available}…")
            else:
                log(f"Instalando AutoFirma {available}…")

            install_args = ["apt-get", "install", "-y"]
            if reinstalar_por_java_tardio:
                install_args.append("--reinstall")
            install_args.append(str(deb))

            self._run(install_args, sudo=True, check=True)

            autofirma_cmd = self.verify_autofirma_installation(log)
            log("AutoFirma instalada/actualizada correctamente.")
            info_msg = "AutoFirma está instalada, Java y NSS Tools están disponibles y la aplicación responde correctamente."
            return info_msg, autofirma_cmd
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    # ---------- NSS ----------

    def ensure_nss(self, log=lambda t: None):
        if NSS_DIR.exists():
            log(f"NSS ya existe: {NSS_DIR}")
            log("No se recrea, no se borra y no se modifica.")
            return "Existe (no se modifica)"

        log("Creando almacén NSS nuevo con contraseña vacía…")
        NSS_DIR.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(
            ["certutil", "-N", "-d", f"sql:{NSS_DIR}", "--empty-password"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or "No se pudo crear el almacén NSS.")
        os.chmod(NSS_DIR, 0o700)
        log(f"NSS creado: {NSS_DIR}")
        return "Creado (contraseña vacía)"

    def nss_fingerprints(self, db_dir=None):
        if db_dir is None:
            db_dir = NSS_DIR
        if not Path(db_dir).is_dir():
            return []

        p = subprocess.run(
            ["certutil", "-L", "-d", f"sql:{db_dir}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if p.returncode != 0:
            return []

        names = []
        for line in p.stdout.splitlines()[3:]:
            line = line.rstrip()
            if not line:
                continue
            name = re.sub(r"\s+[A-Za-z,]+$", "", line).strip()
            if name and name != "Certificate Nickname":
                names.append(name)

        result = []
        for name in names:
            try:
                c = subprocess.run(
                    ["certutil", "-L", "-d", f"sql:{db_dir}", "-n", name, "-a"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                )
                x = subprocess.run(
                    ["openssl", "x509", "-noout", "-fingerprint", "-sha256"],
                    input=c.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                )
                line = x.stdout.decode(errors="replace").strip()
                if "=" in line:
                    fp = line.split("=", 1)[1].replace(":", "").upper()
                    result.append((name, fp))
            except Exception:
                continue
        return result

    def find_fnmt_status(self):
        if not NSS_DIR.is_dir():
            return "NSS no existe"
        try:
            items = self.nss_fingerprints()
            fnmt = [
                name for name, _ in items
                if "FNMT" in name.upper() or "CERES" in name.upper() or "AC FNMT" in name.upper()
            ]
            if fnmt:
                return f"Detectado ({len(fnmt)} certificado(s) relacionado(s))"
            return "NSS disponible; no se ha identificado FNMT por nombre"
        except Exception:
            return "NSS disponible"

    # ---------- Certificado FNMT ----------

    def cert_fingerprint(self, cert_file, password):
        # La contraseña entra por stdin: no se guarda en disco ni aparece
        # como argumento de proceso.
        p = subprocess.run(
            ["openssl", "pkcs12", "-in", str(cert_file), "-clcerts", "-nokeys", "-passin", "stdin"],
            input=(password + "\n").encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if p.returncode != 0:
            raise RuntimeError("No se pudo abrir el certificado. Comprueba la contraseña.")
        x = subprocess.run(
            ["openssl", "x509", "-noout", "-fingerprint", "-sha256"],
            input=p.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        line = x.stdout.decode(errors="replace").strip()
        if "=" not in line:
            raise RuntimeError("No se pudo obtener la huella SHA-256.")
        return line.split("=", 1)[1].replace(":", "").upper()

    def import_certificate(self, cert_file, secret, log=lambda t: None, status=lambda n, v: None):
        log(f"Certificado seleccionado: {cert_file}")
        log("Calculando huella SHA-256…")
        fingerprint = self.cert_fingerprint(cert_file, secret)
        log(f"SHA-256: {fingerprint}")

        existing = None
        for name, fp in self.nss_fingerprints():
            if fp == fingerprint:
                existing = name
                break

        if existing:
            status("cert_estado", f"Ya existe en NSS: {existing}")
            log(f"El certificado ya existe en NSS ({existing}). No se importará de nuevo.")
            return "El certificado ya estaba instalado en NSS."

        log("El certificado no existe en NSS.")
        log("Importándolo…")
        p = subprocess.run(
            ["pk12util", "-d", f"sql:{NSS_DIR}", "-i", str(cert_file), "-w", "/dev/stdin"],
            input=(secret + "\n").encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if p.returncode != 0:
            detail = (p.stderr or p.stdout).decode(errors="replace").strip()
            raise RuntimeError("No se pudo importar el certificado." + (f"\n\n{detail}" if detail else ""))

        found = any(fp == fingerprint for _, fp in self.nss_fingerprints())
        if not found:
            raise RuntimeError("La importación terminó, pero no se pudo verificar la huella en NSS.")

        status("cert_estado", "Importado y verificado")
        log("Certificado importado y verificado correctamente.")
        return "El certificado FNMT se ha importado correctamente."

    # ---------- Confianza del certificado local en navegadores ----------

    def _nss_trust_targets(self):
        """Devuelve [(etiqueta, directorio_sql_nss), ...] a actualizar:
        ~/.pki/nssdb (Chrome/Chromium/Brave) + cada perfil de Firefox
        encontrado (Firefox mantiene su propio cert9.db por perfil)."""
        targets = []
        if NSS_DIR.is_dir():
            targets.append(("NSS compartido (Chrome/Chromium/Brave)", NSS_DIR))

        xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME", "") or (Path.home() / ".config"))
        firefox_roots = [
            xdg_config_home / "mozilla" / "firefox",
            Path.home() / ".mozilla" / "firefox",
        ]

        vistos = set()
        for firefox_root in firefox_roots:
            if not firefox_root.is_dir():
                continue
            for profile_dir in sorted(firefox_root.glob("*")):
                resolved = profile_dir.resolve()
                if resolved in vistos:
                    continue
                if (profile_dir / "cert9.db").is_file():
                    vistos.add(resolved)
                    targets.append((f"Firefox: {profile_dir.name}", profile_dir))

        return targets

    def _x509_fingerprint(self, cert_path):
        x = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-fingerprint", "-sha256"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if x.returncode != 0:
            detail = x.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"No se pudo leer el certificado {cert_path}." + (f"\n\n{detail}" if detail else ""))
        line = x.stdout.decode(errors="replace").strip()
        if "=" not in line:
            raise RuntimeError(f"No se pudo obtener la huella SHA-256 de {cert_path}.")
        return line.split("=", 1)[1].replace(":", "").upper()

    def _nss_trust_flags(self, db_dir):
        """Nickname -> primer campo (SSL) de sus Trust Attributes: 'C'/'T' =
        CA de confianza SSL; 'c' = CA válida pero SIN marcar; '' = sin datos."""
        p = subprocess.run(
            ["certutil", "-L", "-d", f"sql:{db_dir}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if p.returncode != 0:
            return {}
        flags = {}
        for line in p.stdout.splitlines()[3:]:
            line = line.rstrip()
            if not line:
                continue
            m = re.match(r"^(.*\S)\s+([A-Za-z,]*)$", line)
            if not m:
                continue
            name, trust = m.group(1).strip(), m.group(2)
            if name == "Certificate Nickname":
                continue
            flags[name] = trust.split(",")[0] if trust else ""
        return flags

    def trust_root_cert(self, log=lambda t: None, status=lambda n, v: None):
        if not AUTOFIRMA_ROOT_CERT.is_file():
            raise RuntimeError(
                f"No se encuentra {AUTOFIRMA_ROOT_CERT}. ¿Está AutoFirma instalada? "
                "Pulsa antes 'Instalar / actualizar AutoFirma'."
            )

        targets = self._nss_trust_targets()
        if not targets:
            raise RuntimeError(
                "No se ha encontrado ningún almacén NSS (ni "
                f"{NSS_DIR} ni perfiles de Firefox con cert9.db). Pulsa antes "
                "'Comprobar NSS' y abre Firefox al menos una vez para que exista su perfil."
            )

        fingerprint = self._x509_fingerprint(AUTOFIRMA_ROOT_CERT)
        log(f"Huella SHA-256 de {AUTOFIRMA_ROOT_CERT.name}: {fingerprint}")

        importados = 0
        ya_presentes = 0
        for label, db_dir in targets:
            trust_flags = self._nss_trust_flags(db_dir)
            existing_name = next((n for n, fp in self.nss_fingerprints(db_dir) if fp == fingerprint), None)
            ya_confia = existing_name is not None and (
                "C" in trust_flags.get(existing_name, "") or "T" in trust_flags.get(existing_name, "")
            )
            if ya_confia:
                log(f"{label}: ya confía en este certificado.")
                ya_presentes += 1
                continue

            if existing_name is not None:
                log(f"{label}: «{existing_name}» ya estaba en el almacén pero sin confianza SSL; corrigiendo…")
                p = subprocess.run(
                    ["certutil", "-M", "-n", existing_name, "-t", "C,,", "-d", f"sql:{db_dir}"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            else:
                log(f"{label}: importando {AUTOFIRMA_ROOT_CERT.name}…")
                p = subprocess.run(
                    [
                        "certutil", "-A", "-n", AUTOFIRMA_ROOT_NICKNAME, "-t", "C,,",
                        "-i", str(AUTOFIRMA_ROOT_CERT), "-d", f"sql:{db_dir}",
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )

            if p.returncode != 0:
                detail = (p.stderr or p.stdout).decode(errors="replace").strip()
                raise RuntimeError(f"No se pudo importar/corregir el certificado en {label}." + (f"\n\n{detail}" if detail else ""))

            nombre_real = next((n for n, fp in self.nss_fingerprints(db_dir) if fp == fingerprint), None)
            if nombre_real is None:
                raise RuntimeError(f"La operación en {label} terminó sin error, pero no se pudo verificar después por huella.")

            trust_after = self._nss_trust_flags(db_dir).get(nombre_real, "")
            if "C" not in trust_after and "T" not in trust_after:
                raise RuntimeError(
                    f"La operación en {label} terminó sin error, pero «{nombre_real}» sigue sin "
                    f"confianza SSL (trust actual: '{trust_after or '(vacío)'}')."
                )
            log(f"{label}: «{nombre_real}» confiado y verificado (trust: {trust_after}).")
            importados += 1

        resumen = f"{importados} almacén(es) actualizado(s), {ya_presentes} ya lo tenían."
        status("trust_estado", resumen)
        log(resumen)
        return (
            "Certificado local de AutoFirma confiado en los navegadores detectados. "
            "Cierra por completo Firefox (y Chrome/Chromium/Brave si los usas) antes "
            "de volver a probar la firma web."
        )


# =======================================================================
# Hilo de trabajo: equivalente Qt de threading.Thread + queue.Queue del
# original. El hilo emite señales; Qt las entrega al hilo principal de
# forma segura y automática (sin polling).
# =======================================================================

class Worker(QThread):
    log = pyqtSignal(str)
    status = pyqtSignal(str, str)
    done = pyqtSignal()
    error = pyqtSignal(str)
    info = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.fn(self)
        except Exception as exc:
            self.error.emit(str(exc))


class PasswordDialog(QDialog):
    """Equivalente Qt del Toplevel de _ask_password() en la versión Tkinter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Contraseña del certificado")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Introduce la contraseña del certificado FNMT:"))

        self.entry = QLineEdit()
        self.entry.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.entry)

        buttons = QHBoxLayout()
        cancel_btn = QPushButton("Cancelar")
        accept_btn = QPushButton("Importar")
        cancel_btn.clicked.connect(self.reject)
        accept_btn.clicked.connect(self.accept)
        buttons.addStretch()
        buttons.addWidget(cancel_btn)
        buttons.addWidget(accept_btn)
        layout.addLayout(buttons)

        self.entry.returnPressed.connect(self.accept)
        self.entry.setFocus()

    def password(self):
        # Se lee y se limpia el campo en cuanto se usa: la contraseña no
        # queda residente en el widget más tiempo del necesario.
        value = self.entry.text()
        self.entry.clear()
        return value


class AutoFirmaWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.core = AutoFirmaCore()
        self.setWindowTitle("Instalador de AutoFirma — Debian (PyQt6)")
        self.resize(1150, 950)
        self.setMinimumSize(900, 720)
        self.worker = None

        self._build_ui()
        self._refresh_status()

    # ---------- UI ----------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 18, 18, 18)

        title = QLabel("AutoFirma para Debian")
        title.setFont(QFont("Sans", 22, QFont.Weight.Bold))
        outer.addWidget(title)
        outer.addWidget(QLabel("Instalación, actualización, NSS y certificado FNMT en una sola aplicación."))

        # Etiquetas de estado (mismos nombres que antes: _on_status las
        # localiza por getattr(self, f"lbl_{name}")).
        self.lbl_version_instalada = QLabel("Comprobando…")
        self.lbl_version_oficial = QLabel("No comprobada")
        self.lbl_nss_estado = QLabel("Comprobando…")
        self.lbl_cert_estado = QLabel("No comprobado")
        self.lbl_trust_estado = QLabel("No comprobado")

        def make_card(head, desc, rows, button_text, slot):
            box = QGroupBox(head)
            box.setMinimumHeight(250)
            lay = QVBoxLayout(box)
            lay.setContentsMargins(14, 14, 14, 14)
            lay.setSpacing(6)
            d = QLabel(desc)
            d.setWordWrap(True)
            lay.addWidget(d)
            lay.addSpacing(6)
            # Etiqueta y valor en dos QLabel apilados (sin QFormLayout): el
            # valor reserva alto para 3 líneas, así el texto ajustado nunca
            # se monta ni se recorta.
            alto_valor = self.fontMetrics().lineSpacing() * 3 + 6
            for label, widget in rows:
                cap = QLabel(label)
                cap.setStyleSheet("font-weight: bold;")
                lay.addWidget(cap)
                widget.setWordWrap(True)
                widget.setMinimumHeight(alto_valor)
                widget.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
                lay.addWidget(widget)
            lay.addStretch()
            btn = QPushButton(button_text)
            btn.setMinimumHeight(36)
            btn.clicked.connect(slot)
            lay.addWidget(btn)
            return box, btn

        card1, self.btn_install = make_card(
            "AutoFirma", "Instala o actualiza desde la página oficial",
            [("Instalada:", self.lbl_version_instalada),
             ("Versión oficial:", self.lbl_version_oficial)],
            "Instalar / actualizar AutoFirma", self.install_or_update,
        )
        card2, self.btn_nss = make_card(
            "NSS", "Crea o revisa ~/.pki/nssdb",
            [("Almacén NSS:", self.lbl_nss_estado)],
            "Comprobar NSS", self.ensure_nss,
        )
        card3, self.btn_cert = make_card(
            "Certificado", "Añade tu .p12 / .pfx al almacén",
            [("Certificado FNMT:", self.lbl_cert_estado)],
            "Importar certificado FNMT", self.import_certificate,
        )
        card4, self.btn_trust = make_card(
            "Navegadores", "Confía en AutoFirma ROOT",
            [("Cert. en navegadores:", self.lbl_trust_estado)],
            "Confiar cert. en navegadores", self.trust_root_cert,
        )

        grid = QGridLayout()
        for i, card in enumerate((card1, card2, card3, card4)):
            grid.addWidget(card, i // 2, i % 2)
        grid.setSpacing(10)
        outer.addLayout(grid, stretch=2)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Monospace", 12))
        self.log_view.setMinimumHeight(260)
        outer.addWidget(self.log_view, stretch=3)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        outer.addWidget(self.progress)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Fuente: firmaelectronica.gob.es"))
        bottom.addStretch()
        exit_btn = QPushButton("Salir")
        exit_btn.clicked.connect(self.close)
        bottom.addWidget(exit_btn)
        outer.addLayout(bottom)

    def _append_log(self, text):
        self.log_view.appendPlainText(text.rstrip())

    def _set_buttons(self, enabled):
        for b in (self.btn_install, self.btn_nss, self.btn_cert, self.btn_trust):
            b.setEnabled(enabled)

    def _refresh_status(self):
        try:
            installed = self.core.get_installed_version()
            self.lbl_version_instalada.setText(installed or "No instalada")
        except Exception:
            self.lbl_version_instalada.setText("No disponible")

        self.lbl_nss_estado.setText("Existe (no se modifica)" if NSS_DIR.is_dir() else "No existe")
        self.lbl_cert_estado.setText(self.core.find_fnmt_status())

    # ---------- Ejecución en segundo plano ----------

    def _run_async(self, fn):
        if self.worker is not None and self.worker.isRunning():
            return
        self._set_buttons(False)
        self.progress.show()
        self.worker = Worker(fn)
        self.worker.log.connect(self._append_log)
        self.worker.status.connect(self._on_status)
        self.worker.error.connect(self._on_error)
        self.worker.info.connect(self._on_info)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def _on_status(self, name, value):
        widget = getattr(self, f"lbl_{name}", None)
        if widget is not None:
            widget.setText(value)

    def _on_done(self):
        self.progress.hide()
        self._set_buttons(True)
        self._refresh_status()

    def _on_error(self, message):
        self.progress.hide()
        self._set_buttons(True)
        self._append_log("ERROR: " + message)
        QMessageBox.critical(self, "AutoFirma", message)
        self._refresh_status()

    def _on_info(self, message):
        QMessageBox.information(self, "AutoFirma", message)

    # ---------- Acciones ----------

    def install_or_update(self):
        def task(worker):
            info_msg, autofirma_cmd = self.core.install_or_update(
                log=worker.log.emit, status=worker.status.emit,
            )
            if info_msg is None:
                # Ya estaba en su última versión y no hacía falta tocar nada.
                worker.done.emit()
                return

            worker.info.emit(info_msg)
            if autofirma_cmd:
                try:
                    subprocess.Popen(
                        [autofirma_cmd], stdout=subprocess.DEVNULL,                        stderr=subprocess.DEVNULL, start_new_session=True,
                    )
                    worker.log.emit("Abriendo AutoFirma…")
                except Exception as launch_exc:
                    worker.log.emit(f"AutoFirma está instalada pero no se pudo abrir automáticamente: {launch_exc}")
            worker.done.emit()

        self._run_async(task)

    def ensure_nss(self):
        def task(worker):
            estado = self.core.ensure_nss(log=worker.log.emit)
            worker.status.emit("nss_estado", estado)
            worker.done.emit()

        self._run_async(task)

    def import_certificate(self):
        if not NSS_DIR.is_dir():
            resp = QMessageBox.question(
                self, "Almacén NSS",
                "No existe el almacén NSS.\n\n¿Quieres crearlo ahora con contraseña vacía?",
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
            self.ensure_nss()
            QMessageBox.information(
                self, "NSS",
                "El NSS se está creando. Cuando termine, pulsa de nuevo «Importar certificado FNMT».",
            )
            return

        cert_file, _ = QFileDialog.getOpenFileName(
            self, "Seleccionar certificado FNMT", "",
            "Certificado PKCS#12 (*.pfx *.p12);;Todos los archivos (*)",
        )
        if not cert_file:
            return

        if Path(cert_file).suffix.lower() not in (".pfx", ".p12"):
            QMessageBox.critical(self, "Certificado", "Selecciona un archivo .pfx o .p12.")
            return

        dialog = PasswordDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        # Se copia y se limpia la referencia del diálogo aquí, no dentro del
        # closure de task(): igual que en la versión Tkinter, evita dejar la
        # contraseña viva más tiempo del necesario.
        secret = dialog.password()

        def task(worker):
            info_msg = self.core.import_certificate(
                cert_file, secret, log=worker.log.emit, status=worker.status.emit,
            )
            worker.info.emit(info_msg)
            worker.done.emit()

        self._run_async(task)

    def trust_root_cert(self):
        def task(worker):
            info_msg = self.core.trust_root_cert(log=worker.log.emit, status=worker.status.emit)
            worker.info.emit(info_msg)
            worker.done.emit()

        self._run_async(task)


def main():
    app = QApplication([])
    window = AutoFirmaWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()