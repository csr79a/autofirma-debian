#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import os
import ssl
from datetime import datetime
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

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
# Certificado de confianza que AutoFirma genera para su comunicacion SSL
# local (127.0.0.1). El paquete Debian no lo registra de forma fiable en
# todos los navegadores (confirmado: SAF_04 en Brave, "Cargando" infinito
# en Firefox), a diferencia del launcher de CachyOS/AUR.
AUTOFIRMA_ROOT_CERT = Path("/usr/lib/Autofirma/Autofirma_ROOT.cer")
AUTOFIRMA_ROOT_NICKNAME = "AutoFirma ROOT"


class AutoFirmaGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Instalador de AutoFirma — Debian")
        self.geometry("820x620")
        self.minsize(760, 560)

        self.events = queue.Queue()
        self.busy = False

        self.version_instalada = tk.StringVar(value="Comprobando…")
        self.version_oficial = tk.StringVar(value="No comprobada")
        self.nss_estado = tk.StringVar(value="Comprobando…")
        self.cert_estado = tk.StringVar(value="No comprobado")
        self.trust_estado = tk.StringVar(value="No comprobado")

        self._build_ui()
        self.after(100, self._drain_events)
        self._refresh_status()

    # ---------- UI ----------

    def _build_ui(self):
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="AutoFirma para Debian",
            font=("TkDefaultFont", 18, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            outer,
            text="Instalación, actualización, NSS y certificado FNMT en una sola aplicación.",
        ).pack(anchor="w", pady=(2, 16))

        status = ttk.LabelFrame(outer, text="Estado", padding=12)
        status.pack(fill="x")

        rows = [
            ("AutoFirma instalada:", self.version_instalada),
            ("Versión oficial:", self.version_oficial),
            ("Almacén NSS:", self.nss_estado),
            ("Certificado FNMT:", self.cert_estado),
            ("Cert. en navegadores:", self.trust_estado),
        ]
        for i, (label, variable) in enumerate(rows):
            ttk.Label(status, text=label).grid(row=i, column=0, sticky="w", padx=(0, 12), pady=4)
            ttk.Label(status, textvariable=variable).grid(row=i, column=1, sticky="w", pady=4)

        actions = ttk.LabelFrame(outer, text="Acciones", padding=12)
        actions.pack(fill="x", pady=14)

        self.install_button = ttk.Button(
            actions,
            text="Instalar / actualizar AutoFirma",
            command=self.install_or_update,
        )
        self.install_button.pack(side="left", padx=(0, 8))

        self.nss_button = ttk.Button(
            actions,
            text="Comprobar NSS",
            command=self.ensure_nss,
        )
        self.nss_button.pack(side="left", padx=8)

        self.cert_button = ttk.Button(
            actions,
            text="Importar certificado FNMT",
            command=self.import_certificate,
        )
        self.cert_button.pack(side="left", padx=8)

        self.trust_button = ttk.Button(
            actions,
            text="Confiar cert. en navegadores",
            command=self.trust_root_cert,
        )
        self.trust_button.pack(side="left", padx=8)

        log_frame = ttk.LabelFrame(outer, text="Progreso", padding=8)
        log_frame.pack(fill="both", expand=True)

        self.log = tk.Text(log_frame, height=16, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(10, 0))

        ttk.Label(
            bottom,
            text="Fuente: firmaelectronica.gob.es",
        ).pack(side="left")

        ttk.Button(bottom, text="Salir", command=self.destroy).pack(side="right")

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._append_log(value)
                elif kind == "status":
                    name, value2 = value
                    getattr(self, name).set(value2)
                elif kind == "done":
                    self.busy = False
                    self._set_buttons(True)
                    self._refresh_status()
                elif kind == "error":
                    self.busy = False
                    self._set_buttons(True)
                    self._append_log("ERROR: " + value)
                    messagebox.showerror("AutoFirma", value)
                    self._refresh_status()
                elif kind == "info":
                    messagebox.showinfo("AutoFirma", value)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _set_buttons(self, enabled):
        state = "normal" if enabled else "disabled"
        self.install_button.configure(state=state)
        self.nss_button.configure(state=state)
        self.cert_button.configure(state=state)
        self.trust_button.configure(state=state)

    def _run_async(self, target):
        if self.busy:
            return
        self.busy = True
        self._set_buttons(False)
        threading.Thread(target=target, daemon=True).start()

    def log_msg(self, text):
        self.events.put(("log", text))

    def set_status(self, name, value):
        self.events.put(("status", (name, value)))

    # ---------- Command helpers ----------

    def _run(self, args, *, sudo=False, input_text=None, check=True):
        if sudo:
            args = ["pkexec"] + list(args)

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
                "No se encuentra pkexec. Instala policykit-1 o ejecuta la aplicación "
                "desde un entorno Debian con PolicyKit disponible."
            )

    # ---------- Status ----------

    def _refresh_status(self):
        try:
            installed = self.get_installed_version()
            self.version_instalada.set(installed or "No instalada")
        except Exception:
            self.version_instalada.set("No disponible")

        self.nss_estado.set(
            "Existe (no se modifica)" if NSS_DIR.is_dir() else "No existe"
        )

        self.cert_estado.set(self.find_fnmt_status())

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

    # Orden de preferencia de JRE: se instala la primera que APT ofrezca
    # como candidata en este sistema. Debian retira paquetes openjdk-N
    # antiguos de testing/unstable cuando promueve un nuevo valor por
    # defecto, así que no se puede asumir que una versión concreta siga
    # empaquetada; se decide en tiempo de ejecución, no se hardcodea.
    JRE_CANDIDATES = ["openjdk-17-jre", "openjdk-21-jre"]

    def _apt_has_candidate(self, package):
        p = subprocess.run(
            ["apt-cache", "policy", package],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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

    def ensure_runtime_dependencies(self):
        """Instala las dependencias que el .deb oficial no instala por sí solo.

        Devuelve True si en esta llamada se ha instalado una JRE que antes
        no estaba presente. Es relevante para quien la invoca: si AutoFirma
        ya estaba instalado y la JRE llega DESPUÉS, el .deb de AutoFirma
        generó (o intentó generar) en su día el almacén SSL para el
        WebSocket local (puerto 63117) sin Java disponible, y ese paso no
        se reintenta solo. Instalar Java más tarde no lo repara por sí
        mismo; hace falta reinstalar el paquete para que su postinst
        vuelva a ejecutarse. Ver SAF_45 (KeyStoreException: No se
        encuentra el almacen para el cifrado de la comunicacion SSL).
        """
        self._require_pkexec()

        necesita_java = not self._command_exists("java")
        if not necesita_java:
            # AutoFirma necesita una JRE completa, no solo headless.
            p = subprocess.run(["java", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            text = (p.stdout + p.stderr).decode(errors="replace")
            necesita_java = "OpenJDK" not in text and "Java" not in text

        necesita_nss = not self._command_exists("certutil")
        necesita_curl = not self._command_exists("curl")

        if not (necesita_java or necesita_nss or necesita_curl):
            self.log_msg("Java, NSS Tools y curl ya están disponibles.")
            return False

        # El índice APT se refresca ANTES de decidir qué paquete de JRE
        # instalar: decidirlo contra una caché desactualizada podría
        # elegir un paquete que ya no tiene candidato real.
        self.log_msg("Actualizando índice de APT…")
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

        self.log_msg("Instalando dependencias necesarias: " + ", ".join(missing))
        p = self._run(["apt-get", "install", "-y"] + missing, sudo=True, check=False)
        if p.returncode != 0:
            detail = p.stderr.decode(errors="replace").strip()
            raise RuntimeError("No se pudieron instalar las dependencias de AutoFirma." + (f"\n\n{detail}" if detail else ""))

        if not self._command_exists("java"):
            raise RuntimeError("Java sigue sin estar disponible después de la instalación.")
        if not self._command_exists("certutil"):
            raise RuntimeError("NSS Tools sigue sin estar disponible después de la instalación.")
        self.log_msg("Dependencias instaladas correctamente.")
        p = subprocess.run(["java", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        version_text = (p.stdout + p.stderr).decode(errors="replace").strip().splitlines()
        if version_text:
            self.log_msg("Java: " + version_text[0])

        return necesita_java

    def verify_autofirma_installation(self):
        """No da la instalación por correcta hasta comprobar Java, NSS y AutoFirma."""
        if not self._command_exists("java"):
            raise RuntimeError("AutoFirma está instalada pero Java no está disponible.")
        if not self._command_exists("certutil"):
            raise RuntimeError("AutoFirma está instalada pero faltan NSS Tools (certutil).")
        autofirma = shutil.which("AutoFirma") or shutil.which("autofirma")
        if not autofirma:
            raise RuntimeError("El paquete se instaló, pero no existe el ejecutable /usr/bin/AutoFirma.")
        if not Path("/usr/lib/Autofirma").exists():
            raise RuntimeError("El paquete se instaló, pero falta /usr/lib/Autofirma.")
        p = subprocess.run([autofirma, "-help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        output = (p.stdout + p.stderr).decode(errors="replace")
        if p.returncode != 0:
            raise RuntimeError("AutoFirma está instalada pero no puede ejecutarse con la Java disponible.\n\n" + output[-2000:])
        self.log_msg("AutoFirma responde correctamente con Java y NSS Tools.")
        return autofirma

    # ---------- HTTPS / official download ----------

    def _system_time_ok(self):
        # No intentamos corregir el reloj automáticamente: solo detectamos
        # fechas manifiestamente incorrectas que pueden provocar TLS errors.
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

    def _check_official_https(self):
        ok_time, time_msg = self._system_time_ok()
        self.log_msg(time_msg)
        if not ok_time:
            raise RuntimeError(
                time_msg +
                "\n\nCorrige la fecha/hora del sistema y vuelve a intentarlo."
            )

        cafile = self._ca_bundle()
        if not cafile:
            self.log_msg("No se encuentra el bundle CA de Debian.")
            self.log_msg("Intentando reparar ca-certificates…")
            self._repair_ca_certificates()
            cafile = self._ca_bundle()

        if not cafile:
            raise RuntimeError(
                "No se pudo localizar el almacén CA después de reparar "
                "ca-certificates."
            )

        self.log_msg(f"Usando certificados CA del sistema: {cafile}")

        context = self._https_context()

        request = urllib.request.Request(
            OFFICIAL_PAGE,
            headers={"User-Agent": "autofirma-debian-installer/1.1"},
        )

        try:
            with urllib.request.urlopen(
                request,
                context=context,
                timeout=30,
            ) as response:
                data = response.read()
        except ssl.SSLCertVerificationError as exc:
            # Nunca hacemos fallback a verify=False. Si el servidor oficial
            # presenta un certificado caducado, el problema está en el
            # servidor y no en el almacén CA local.
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
            raise RuntimeError(
                "Falló la conexión TLS con la página oficial.\n\n"
                f"Detalle: {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"No se pudo acceder a la página oficial:\n{exc}"
            ) from exc

        return data.decode("utf-8", errors="replace")

    def _repair_ca_certificates(self):
        self._require_pkexec()

        # Debian gestiona el bundle mediante ca-certificates y
        # update-ca-certificates. No descargamos certificados CA de terceros.
        p = self._run(
            ["apt-get", "update"],
            sudo=True,
            check=False,
        )
        if p.returncode != 0:
            detail = p.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                "No se pudo actualizar el índice de paquetes para reparar "
                "ca-certificates."
                + (f"\n\n{detail}" if detail else "")
            )

        p = self._run(
            ["apt-get", "install", "--reinstall", "-y", "ca-certificates"],
            sudo=True,
            check=False,
        )
        if p.returncode != 0:
            detail = p.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                "No se pudo reinstalar ca-certificates."
                + (f"\n\n{detail}" if detail else "")
            )

        p = self._run(
            ["update-ca-certificates"],
            sudo=True,
            check=False,
        )
        if p.returncode != 0:
            detail = p.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                "No se pudo reconstruir el almacén CA."
                + (f"\n\n{detail}" if detail else "")
            )

        self.log_msg("ca-certificates reparado/actualizado correctamente.")

    def get_official_deb(self):
        self.log_msg("Consultando la página oficial de AutoFirma…")
        try:
            html = self._check_official_https()
        except RuntimeError as exc:
            message = str(exc).lower()
            if "certificado https caducado" not in message and "certificate has expired" not in message:
                raise

            # Recuperación controlada: el portal oficial está presentando un
            # certificado caducado. No aceptamos certificados arbitrarios.
            # Continuamos únicamente con la URL oficial exacta y verificamos
            # después el SHA-256 completo del archivo descargado.
            self.log_msg(
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
            if ("autofirma" in low and "debian" in low and low.endswith(".zip")):
                candidates.append(absolute)

        if not candidates:
            raise RuntimeError(
                "La página oficial respondió correctamente, pero no se encontró "
                "el paquete Debian de AutoFirma."
            )

        # Para una descarga descubierta dinámicamente no inventamos una huella.
        return candidates[0], "zip", None, None

    def download_deb(self):
        url, kind, expected_sha256, expected_size = self.get_official_deb()
        self.log_msg(f"Paquete oficial: {url}")

        temp_dir = Path(tempfile.mkdtemp(prefix="autofirma-"))
        try:
            filename = Path(urllib.parse.urlparse(url).path).name or "autofirma-package"
            target = temp_dir / filename

            if expected_sha256:
                # El certificado TLS del portal está caducado. Para no bloquear
                # la instalación de la versión oficial actual, usamos curl solo
                # contra esta URL exacta y comprobamos tamaño + SHA-256 antes de
                # abrir el ZIP. Un archivo que no coincida se rechaza.
                if not self._command_exists("curl"):
                    raise RuntimeError(
                        "Se necesita curl para la descarga de recuperación segura. "
                        "Instálalo con: sudo apt install curl"
                    )
                self.log_msg(
                    "AVISO: esta descarga se realiza con --insecure (verificación "
                    "TLS de cadena/hostname/caducidad DESACTIVADA para esta URL "
                    "exacta), porque el certificado del portal está caducado. "
                    "La integridad del archivo se garantiza después comparando "
                    "tamaño exacto y SHA-256 contra los valores oficiales fijados "
                    "en el código (no por la conexión TLS)."
                )
                self.log_msg("Descargando la versión oficial fijada de AutoFirma 1.9…")
                cmd = [
                    "curl", "--location", "--fail", "--silent", "--show-error",
                    "--insecure", "--output", str(target), url,
                ]
                p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if p.returncode != 0:
                    raise RuntimeError(
                        "No se pudo descargar el paquete oficial de AutoFirma.\n\n"
                        f"curl: {p.stderr.strip()}"
                    )

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
                self.log_msg("SHA-256 verificado correctamente.")
            else:
                context = self._https_context()
                request = urllib.request.Request(
                    url, headers={"User-Agent": "autofirma-debian-installer/1.2"}
                )
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

    # ---------- Install/update ----------

    def install_or_update(self):
        def worker():
            temp_dir = None
            try:
                self._require_pkexec()
                self.log_msg("Comprobando AutoFirma…")

                installed = self.get_installed_version()
                java_recien_instalado = self.ensure_runtime_dependencies()
                if installed:
                    self.log_msg(f"Versión instalada: {installed}")
                else:
                    self.log_msg("AutoFirma no está instalada.")

                deb, temp_dir = self.download_deb()

                p = subprocess.run(
                    ["dpkg-deb", "-f", str(deb), "Version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=True,
                )
                available = p.stdout.strip()
                self.set_status("version_oficial", available)
                self.log_msg(f"Versión oficial disponible: {available}")

                reinstalar_por_java_tardio = False
                if installed:
                    cmp = subprocess.run(
                        ["dpkg", "--compare-versions", available, "gt", installed]
                    ).returncode
                    eq = subprocess.run(
                        ["dpkg", "--compare-versions", available, "eq", installed]
                    ).returncode

                    if eq == 0:
                        if java_recien_instalado:
                            # AutoFirma ya estaba instalado y Java se acaba
                            # de instalar ahora mismo. El .deb de AutoFirma
                            # genera en su postinst el almacén SSL para el
                            # WebSocket local usando la JRE del sistema; si
                            # entonces no había ninguna, ese almacén quedó
                            # mal generado o ausente y el navegador nunca
                            # podrá hablar con AutoFirma (SAF_45), aunque la
                            # app de escritorio abra sin problema. Instalar
                            # Java después no repara esto por sí solo: hay
                            # que forzar que el postinst se vuelva a
                            # ejecutar con Java ya presente.
                            self.log_msg(
                                f"AutoFirma ya está en su versión más nueva ({installed}), "
                                "pero Java se acaba de instalar en esta misma operación. "
                                "AutoFirma se instaló originalmente sin una JRE disponible, "
                                "lo que puede dejar roto el almacén SSL del WebSocket local "
                                "(error SAF_45 al firmar desde el navegador). "
                                "Se reinstalará el paquete para regenerarlo."
                            )
                            reinstalar_por_java_tardio = True
                        else:
                            self.log_msg(
                                f"AutoFirma ya está en su versión más nueva ({installed}). "
                                "No hay nada que actualizar."
                            )
                            self.events.put(("done", None))
                            return
                    elif cmp != 0:
                        self.log_msg(
                            f"La versión instalada ({installed}) es superior a la "
                            f"descargada ({available}). No se modifica AutoFirma."
                        )
                        self.events.put(("done", None))
                        return
                    else:
                        self.log_msg(f"Actualizando AutoFirma a {available}…")
                else:
                    self.log_msg(f"Instalando AutoFirma {available}…")

                install_args = ["apt-get", "install", "-y"]
                if reinstalar_por_java_tardio:
                    install_args.append("--reinstall")
                install_args.append(str(deb))

                self._run(
                    install_args,
                    sudo=True,
                    check=True,
                )

                autofirma_cmd = self.verify_autofirma_installation()
                self.log_msg("AutoFirma instalada/actualizada correctamente.")
                self.events.put(("info", "AutoFirma está instalada, Java y NSS Tools están disponibles y la aplicación responde correctamente."))
                try:
                    subprocess.Popen([autofirma_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    self.log_msg("Abriendo AutoFirma…")
                except Exception as launch_exc:
                    self.log_msg(f"AutoFirma está instalada pero no se pudo abrir automáticamente: {launch_exc}")
                self.events.put(("done", None))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                if temp_dir:
                    shutil.rmtree(temp_dir, ignore_errors=True)

        self._run_async(worker)

    # ---------- NSS ----------

    def ensure_nss(self):
        def worker():
            try:
                if NSS_DIR.exists():
                    self.log_msg(f"NSS ya existe: {NSS_DIR}")
                    self.log_msg("No se recrea, no se borra y no se modifica.")
                    self.set_status("nss_estado", "Existe (no se modifica)")
                    self.events.put(("done", None))
                    return

                self.log_msg("Creando almacén NSS nuevo con contraseña vacía…")
                NSS_DIR.mkdir(parents=True, exist_ok=True)

                p = subprocess.run(
                    ["certutil", "-N", "-d", f"sql:{NSS_DIR}", "--empty-password"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if p.returncode != 0:
                    raise RuntimeError(p.stderr.strip() or "No se pudo crear el almacén NSS.")

                os.chmod(NSS_DIR, 0o700)
                self.log_msg(f"NSS creado: {NSS_DIR}")
                self.set_status("nss_estado", "Creado (contraseña vacía)")
                self.events.put(("done", None))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self._run_async(worker)

    # ---------- Confianza del certificado local en navegadores ----------

    def _nss_trust_targets(self):
        """Devuelve [(etiqueta, directorio_sql_nss), ...] a actualizar.

        - ~/.pki/nssdb: NSS compartido que usan por defecto Chrome,
          Chromium y Brave en Linux (el mismo almacén donde ya importaste
          tu certificado FNMT).
        - Perfiles de Firefox: Firefox mantiene su propio cert9.db por
          perfil, independiente del NSS del sistema; no basta con confiar
          en el compartido (en Linux, a diferencia de Windows/macOS,
          Firefox no puede configurarse para usar el almacén del sistema).
          Se buscan perfiles en dos sitios porque Firefox ha ido migrando
          su ruta de perfiles al layout XDG:
            - $XDG_CONFIG_HOME/mozilla/firefox (o ~/.config/mozilla/firefox
              si XDG_CONFIG_HOME no está definida): ruta nueva, en uso
              desde versiones recientes de Firefox (confirmado con
              Firefox 156).
            - ~/.mozilla/firefox: ruta clásica, todavía válida para
              perfiles antiguos o instalaciones que no han migrado.
          Se buscan ambas y se combinan sin duplicar (por si algún día
          coexisten perfiles en las dos).
        """
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
        """Huella SHA-256 de un certificado público en PEM/DER (sin clave privada,
        sin contraseña) — Autofirma_ROOT.cer es un certificado suelto, no un .pfx."""
        x = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-fingerprint", "-sha256"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if x.returncode != 0:
            detail = x.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"No se pudo leer el certificado {cert_path}."
                + (f"\n\n{detail}" if detail else "")
            )
        line = x.stdout.decode(errors="replace").strip()
        if "=" not in line:
            raise RuntimeError(f"No se pudo obtener la huella SHA-256 de {cert_path}.")
        return line.split("=", 1)[1].replace(":", "").upper()

    def _nss_trust_flags(self, db_dir):
        """Nickname -> primer campo (SSL) de sus Trust Attributes, tal cual
        certutil -L las reporta: 'C' o 'T' = CA de confianza para SSL;
        'c' = CA válida pero SIN marcar como de confianza; '' = sin datos.

        Antes esta columna se descartaba al listar certificados
        (nss_fingerprints() la recorta con una regexp), así que
        _cert_trusted() solo comparaba huellas y daba por buena la
        confianza con solo que el certificado existiera en el almacén,
        aunque no tuviera el flag puesto. Este helper es el que faltaba
        para comprobar el trust de verdad.
        """
        p = subprocess.run(
            ["certutil", "-L", "-d", f"sql:{db_dir}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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

    def _cert_trusted(self, db_dir, fingerprint):
        trust_flags = self._nss_trust_flags(db_dir)
        for name, fp in self.nss_fingerprints(db_dir):
            if fp != fingerprint:
                continue
            ssl_flag = trust_flags.get(name, "")
            if "C" in ssl_flag or "T" in ssl_flag:
                return True
        return False

    def trust_root_cert(self):
        def worker():
            try:
                if not AUTOFIRMA_ROOT_CERT.is_file():
                    raise RuntimeError(
                        f"No se encuentra {AUTOFIRMA_ROOT_CERT}. "
                        "¿Está AutoFirma instalada? Pulsa antes "
                        "'Instalar / actualizar AutoFirma'."
                    )

                targets = self._nss_trust_targets()
                if not targets:
                    raise RuntimeError(
                        "No se ha encontrado ningún almacén NSS (ni "
                        f"{NSS_DIR} ni perfiles de Firefox con cert9.db). "
                        "Pulsa antes 'Comprobar NSS' y abre Firefox al "
                        "menos una vez para que exista su perfil."
                    )

                fingerprint = self._x509_fingerprint(AUTOFIRMA_ROOT_CERT)
                self.log_msg(f"Huella SHA-256 de {AUTOFIRMA_ROOT_CERT.name}: {fingerprint}")

                importados = 0
                ya_presentes = 0
                for label, db_dir in targets:
                    trust_flags = self._nss_trust_flags(db_dir)
                    existing_name = next(
                        (n for n, fp in self.nss_fingerprints(db_dir) if fp == fingerprint),
                        None,
                    )
                    ya_confia = existing_name is not None and (
                        "C" in trust_flags.get(existing_name, "")
                        or "T" in trust_flags.get(existing_name, "")
                    )
                    if ya_confia:
                        self.log_msg(f"{label}: ya confía en este certificado.")
                        ya_presentes += 1
                        continue

                    if existing_name is not None:
                        # El certificado ya estaba en el almacén (a veces lo
                        # deja así el propio .deb de AutoFirma) pero sin el
                        # flag de confianza SSL. Se corrige su trust en vez
                        # de reimportarlo, para no crear un duplicado con el
                        # mismo nickname.
                        self.log_msg(
                            f"{label}: «{existing_name}» ya estaba en el "
                            "almacén pero sin confianza SSL; corrigiendo…"
                        )
                        p = subprocess.run(
                            [
                                "certutil", "-M",
                                "-n", existing_name,
                                "-t", "C,,",
                                "-d", f"sql:{db_dir}",
                            ],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                    else:
                        # -n es solo una sugerencia de nickname: si el propio
                        # .cer lleva uno incrustado (aquí, "SocketAutoFirma"),
                        # certutil lo usa a él en vez del que le pasamos. No
                        # lo asumimos: verificamos después por huella, no por
                        # nombre.
                        self.log_msg(f"{label}: importando {AUTOFIRMA_ROOT_CERT.name}…")
                        p = subprocess.run(
                            [
                                "certutil", "-A",
                                "-n", AUTOFIRMA_ROOT_NICKNAME,
                                "-t", "C,,",
                                "-i", str(AUTOFIRMA_ROOT_CERT),
                                "-d", f"sql:{db_dir}",
                            ],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )

                    if p.returncode != 0:
                        detail = (p.stderr or p.stdout).decode(errors="replace").strip()
                        raise RuntimeError(
                            f"No se pudo importar/corregir el certificado en {label}."
                            + (f"\n\n{detail}" if detail else "")
                        )

                    nombre_real = next(
                        (n for n, fp in self.nss_fingerprints(db_dir) if fp == fingerprint),
                        None,
                    )
                    if nombre_real is None:
                        raise RuntimeError(
                            f"La operación en {label} terminó sin error, pero "
                            "no se pudo verificar después por huella."
                        )
                    # Verificación real: no basta con que la huella esté;
                    # confirmamos que el flag de confianza SSL quedó puesto,
                    # que es justo lo que antes se daba por hecho sin mirar.
                    trust_after = self._nss_trust_flags(db_dir).get(nombre_real, "")
                    if "C" not in trust_after and "T" not in trust_after:
                        raise RuntimeError(
                            f"La operación en {label} terminó sin error, pero "
                            f"«{nombre_real}» sigue sin confianza SSL "
                            f"(trust actual: '{trust_after or '(vacío)'}')."
                        )
                    self.log_msg(
                        f"{label}: «{nombre_real}» confiado y verificado "
                        f"(trust: {trust_after})."
                    )
                    importados += 1

                resumen = (
                    f"{importados} almacén(es) actualizado(s), "
                    f"{ya_presentes} ya lo tenían."
                )
                self.set_status("trust_estado", resumen)
                self.log_msg(resumen)
                self.events.put((
                    "info",
                    "Certificado local de AutoFirma confiado en los navegadores "
                    "detectados. Cierra por completo Firefox (y Chrome/Chromium/"
                    "Brave si los usas) antes de volver a probar la firma web.",
                ))
                self.events.put(("done", None))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self._run_async(worker)

    # ---------- Certificate ----------

    def select_certificate(self):
        return filedialog.askopenfilename(
            title="Seleccionar certificado FNMT",
            filetypes=[
                ("Certificado PKCS#12", "*.pfx *.p12"),
                ("PFX", "*.pfx"),
                ("PKCS#12", "*.p12"),
                ("Todos los archivos", "*"),
            ],
        )

    def cert_fingerprint(self, cert_file, password):
        # La contraseña entra por stdin: no se guarda en disco ni aparece
        # como argumento de proceso.
        p = subprocess.run(
            [
                "openssl", "pkcs12",
                "-in", str(cert_file),
                "-clcerts", "-nokeys",
                "-passin", "stdin",
            ],
            input=(password + "\n").encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if p.returncode != 0:
            raise RuntimeError("No se pudo abrir el certificado. Comprueba la contraseña.")

        x = subprocess.run(
            [
                "openssl", "x509", "-noout", "-fingerprint", "-sha256"
            ],
            input=p.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        line = x.stdout.decode(errors="replace").strip()
        if "=" not in line:
            raise RuntimeError("No se pudo obtener la huella SHA-256.")
        return line.split("=", 1)[1].replace(":", "").upper()

    def nss_fingerprints(self, db_dir=None):
        if db_dir is None:
            db_dir = NSS_DIR
        if not Path(db_dir).is_dir():
            return []

        p = subprocess.run(
            ["certutil", "-L", "-d", f"sql:{db_dir}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if p.returncode != 0:
            return []

        names = []
        for line in p.stdout.splitlines()[3:]:
            line = line.rstrip()
            if not line:
                continue
            # Las dos columnas finales contienen Trust Attributes.
            name = re.sub(r"\s+[A-Za-z,]+$", "", line).strip()
            if name and name != "Certificate Nickname":
                names.append(name)

        result = []
        for name in names:
            try:
                c = subprocess.run(
                    [
                        "certutil", "-L",
                        "-d", f"sql:{db_dir}",
                        "-n", name,
                        "-a",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                x = subprocess.run(
                    ["openssl", "x509", "-noout", "-fingerprint", "-sha256"],
                    input=c.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
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
                if "FNMT" in name.upper()
                or "CERES" in name.upper()
                or "AC FNMT" in name.upper()
            ]
            if fnmt:
                return f"Detectado ({len(fnmt)} certificado(s) relacionado(s))"
            return "NSS disponible; no se ha identificado FNMT por nombre"
        except Exception:
            return "NSS disponible"

    def import_certificate(self):
        if not NSS_DIR.is_dir():
            if not messagebox.askyesno(
                "Almacén NSS",
                "No existe el almacén NSS.\n\n¿Quieres crearlo ahora con contraseña vacía?",
            ):
                return
            self.ensure_nss()
            messagebox.showinfo(
                "NSS",
                "El NSS se está creando. Cuando termine, pulsa de nuevo "
                "«Importar certificado FNMT».",
            )
            return

        cert_file = self.select_certificate()
        if not cert_file:
            return

        if Path(cert_file).suffix.lower() not in (".pfx", ".p12"):
            messagebox.showerror(
                "Certificado",
                "Selecciona un archivo .pfx o .p12.",
            )
            return

        password = self._ask_password()
        if password is None:
            return

        # Se copia y se limpia la referencia AQUÍ, en el ámbito de
        # import_certificate(), no dentro de worker(): si worker()
        # reasigna 'password', Python la trata como variable local en
        # todo su cuerpo (incluida la lectura anterior a esa reasignación),
        # lo que provocaba un UnboundLocalError fuera del try/except y
        # mataba el hilo en silencio sin emitir ningún evento a la cola.
        secret = password
        password = None

        def worker():
            try:
                self.log_msg(f"Certificado seleccionado: {cert_file}")
                self.log_msg("Calculando huella SHA-256…")

                fingerprint = self.cert_fingerprint(cert_file, secret)
                self.log_msg(f"SHA-256: {fingerprint}")

                existing = None
                for name, fp in self.nss_fingerprints():
                    if fp == fingerprint:
                        existing = name
                        break

                if existing:
                    self.set_status(
                        "cert_estado",
                        f"Ya existe en NSS: {existing}",
                    )
                    self.log_msg(
                        f"El certificado ya existe en NSS ({existing}). "
                        "No se importará de nuevo."
                    )
                    self.events.put(("info", "El certificado ya estaba instalado en NSS."))
                    self.events.put(("done", None))
                    return

                self.log_msg("El certificado no existe en NSS.")
                self.log_msg("Importándolo…")

                # pk12util acepta un fichero de contraseña. /dev/stdin evita
                # crear un archivo temporal que contenga la contraseña.
                p = subprocess.run(
                    [
                        "pk12util",
                        "-d", f"sql:{NSS_DIR}",
                        "-i", str(cert_file),
                        "-w", "/dev/stdin",
                    ],
                    input=(secret + "\n").encode(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                if p.returncode != 0:
                    detail = (p.stderr or p.stdout).decode(errors="replace").strip()
                    raise RuntimeError(
                        "No se pudo importar el certificado."
                        + (f"\n\n{detail}" if detail else "")
                    )

                # Verificación posterior.
                found = any(fp == fingerprint for _, fp in self.nss_fingerprints())
                if not found:
                    raise RuntimeError(
                        "La importación terminó, pero no se pudo verificar "
                        "la huella en NSS."
                    )

                self.set_status("cert_estado", "Importado y verificado")
                self.log_msg("Certificado importado y verificado correctamente.")
                self.events.put(("info", "El certificado FNMT se ha importado correctamente."))
                self.events.put(("done", None))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        # La contraseña vive únicamente durante la operación de importación.
        self._run_async(worker)

    def _ask_password(self):
        dialog = tk.Toplevel(self)
        dialog.title("Contraseña del certificado")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Introduce la contraseña del certificado FNMT:",
        ).pack(anchor="w", pady=(0, 8))

        entry = ttk.Entry(frame, show="•", width=48)
        entry.pack(fill="x")
        entry.focus_set()

        result = {"value": None}

        def accept():
            result["value"] = entry.get()
            entry.delete(0, "end")
            dialog.destroy()

        def cancel():
            entry.delete(0, "end")
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Cancelar", command=cancel).pack(side="right")
        ttk.Button(buttons, text="Importar", command=accept).pack(side="right", padx=8)

        dialog.bind("<Return>", lambda _e: accept())
        dialog.bind("<Escape>", lambda _e: cancel())

        self.wait_window(dialog)
        return result["value"]


if __name__ == "__main__":
    app = AutoFirmaGUI()
    app.mainloop()
