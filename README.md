# AutoFirma Debian — Instalador gráfico

Instalador gráfico para Debian y derivados que reúne en una sola aplicación:

- Instalación de AutoFirma.
- Comprobación de la versión instalada.
- Comprobación de la versión oficial disponible.
- Actualización únicamente cuando existe una versión oficial superior.
- Creación conservadora de `~/.pki/nssdb`.
- No recreación ni sobrescritura de un NSS existente.
- Importación opcional de certificados FNMT `.pfx` / `.p12`.
- Comprobación de huella SHA-256 antes de importar.
- Solicitud gráfica de la contraseña del certificado.
- La contraseña no se guarda en un archivo ni se pasa como argumento del proceso.
- Verificación posterior de la importación.

## Archivos

- `instalar_autofirma_qt.sh`: lanzador principal de la interfaz PyQt6.
- `autofirma_gui_qt.py`: interfaz gráfica y lógica del instalador.
- `autofirma.desktop`: acceso al menú de aplicaciones, configurado para abrir la versión PyQt6.
- `instalar_autofirma.sh`: lanzador de compatibilidad que redirige a la versión PyQt6.

## Uso

Desde el directorio del proyecto:

```bash
chmod +x instalar_autofirma_qt.sh
./instalar_autofirma_qt.sh
```

El instalador utiliza PolicyKit (`pkexec`) para las operaciones que necesitan privilegios de administrador.

## HTTPS y certificados CA

En el flujo normal, la aplicación **no desactiva la verificación TLS** y no utiliza `verify=False` ni un contexto SSL inseguro.

**Excepción documentada:** únicamente cuando el portal oficial presenta un certificado HTTPS caducado (detectado explícitamente, ver más abajo), la descarga de recuperación fijada a la URL oficial exacta de AutoFirma 1.9 se realiza con `curl --insecure` — es decir, sin verificar cadena, hostname ni caducidad en esa conexión concreta. Esto se compensa comprobando después, byte a byte, el tamaño exacto y la huella SHA-256 del archivo descargado contra los valores oficiales fijados en el código; un archivo que no coincida se rechaza y no se instala. La app registra este aviso en el log cada vez que toma esta ruta, para que quede visible.

Antes de consultar o descargar AutoFirma:

1. comprueba la fecha del sistema;
2. utiliza el almacén CA de Debian;
3. si falta `ca-certificates`, intenta repararlo mediante APT;
4. reconstruye el almacén con `update-ca-certificates`;
5. vuelve a realizar la conexión HTTPS verificada.

Si el certificado HTTPS no puede validarse, la aplicación se detiene y muestra el error en lugar de descargar el paquete de forma insegura.

El recurso de AutoFirma debe proceder del dominio oficial `firmaelectronica.gob.es`.

Dependencias principales:

- Python 3
- PyQt6 (paquete `python3-pyqt6` en Debian). Si falta, `instalar_autofirma_qt.sh` lo detecta e intenta instalarlo mediante `pkexec apt install python3-pyqt6` antes de arrancar la GUI. Instalación manual:
  ```bash
  sudo apt install python3-pyqt6
  ```
- PolicyKit / `pkexec` (`pkexec` + `polkitd`). Es el mecanismo que usa la aplicación para pedir la contraseña de administrador con una ventana gráfica (en vez de `sudo` en terminal) al instalar/actualizar AutoFirma o instalar `python3-pyqt6`. Si falta el paquete, o el servicio `polkitd` no está corriendo en la sesión, aparece el error "No se encuentra pkexec. Instala pkexec y polkitd o ejecuta la aplicación desde un entorno Debian con PolicyKit disponible." al pulsar cualquier botón que necesite privilegios. `instalar_autofirma_qt.sh` comprueba esto al arrancar e intenta instalar `pkexec` y `polkitd` automáticamente con `sudo apt install pkexec polkitd` si falta. Nota: si el paquete ya estaba instalado pero el servicio no había arrancado en la sesión actual (por ejemplo, tras una instalación reciente sin reiniciar sesión), el error puede aparecer igualmente hasta cerrar sesión o reiniciar; una actualización del sistema (`apt upgrade`) o un reinicio posterior suele resolverlo sin intervención adicional. Instalación manual:
  ```bash
  sudo apt install pkexec polkitd
  ```
- `curl`/red de Python
- `unzip`
- `openssl`
- `libnss3-tools`
- Java Runtime

La aplicación instala las dependencias del sistema que necesita mediante `apt` cuando procede.

## NSS

El almacén utilizado por este proyecto es:

```text
~/.pki/nssdb
```

Regla conservadora:

- si no existe, se crea con contraseña vacía;
- si ya existe, no se recrea;
- no se borra;
- no se vacía;
- no se inicializa de nuevo.

## Confianza de AutoFirma ROOT en navegadores

La interfaz PyQt6 incluye la acción **«Confiar cert. en navegadores»** para registrar el certificado local `AutoFirma_ROOT.cer` generado por AutoFirma como certificado de confianza SSL en los almacenes NSS de los navegadores detectados.

La acción:

- Utiliza `/usr/lib/Autofirma/Autofirma_ROOT.cer`.
- Comprueba la huella SHA-256 del certificado antes de modificar los almacenes.
- Detecta el almacén NSS compartido `~/.pki/nssdb`, utilizado por Chrome/Chromium/Brave.
- Detecta los perfiles de Firefox que contienen `cert9.db` y trabaja con cada perfil encontrado.
- Si el certificado ya existe pero no tiene confianza SSL, corrige sus atributos de confianza en lugar de duplicarlo.
- Si ya está presente y correctamente confiado, no vuelve a importarlo.
- Verifica después de cada operación que el certificado existe y tiene confianza SSL (`C` o `T`).
- No elimina ni vacía los almacenes NSS existentes.

Después de confiar el certificado, hay que **cerrar completamente Firefox y Chrome/Chromium/Brave** y volver a abrirlos antes de probar una firma web.

## Certificado FNMT

La importación es opcional.

El flujo es:

1. Seleccionar `.pfx` o `.p12`.
2. Introducir la contraseña en un campo oculto.
3. Obtener la huella SHA-256 del certificado.
4. Compararla con los certificados existentes en NSS.
5. Si ya existe, no se importa.
6. Si no existe, se importa mediante `pk12util`.
7. Se verifica nuevamente la huella.

La contraseña no se almacena en disco.

## Fuente

La descarga se obtiene de la página oficial de AutoFirma:

https://firmaelectronica.gob.es/descargas

El proyecto no redistribuye AutoFirma ni incluye el paquete oficial dentro del repositorio.

## Recuperación ante certificado HTTPS caducado del portal oficial

Si `firmaelectronica.gob.es` presenta temporalmente un certificado HTTPS caducado, el instalador no desactiva TLS de forma general ni acepta certificados arbitrarios. Para la versión oficial AutoFirma 1.9 para Debian existe una ruta de recuperación fijada a la URL oficial exacta, con tamaño y SHA-256 esperados; el archivo se rechaza si no coincide. Cuando el portal vuelve a ofrecer TLS válido, el instalador vuelve automáticamente a la consulta HTTPS normal.


## Dependencias de ejecución

El instalador instala automáticamente, si faltan, las dependencias necesarias para ejecutar AutoFirma en Debian:

- Una JRE disponible en APT. Se prueban `openjdk-17-jre` y `openjdk-21-jre` y, como último recurso, `default-jre-headless`. La elección se hace en tiempo de ejecución con `apt-cache policy`, usando `LC_ALL=C` y comprobando el código de salida.
- `libnss3-tools`.
- `curl` para la ruta de recuperación de la descarga oficial mientras el certificado HTTPS del portal esté caducado.

La instalación solo se considera correcta después de comprobar `java`, `certutil`, `/usr/lib/AutoFirma` y `/usr/bin/AutoFirma -help`. Esta última comprobación es la que realmente certifica que la JRE instalada (17 o 21) es compatible con AutoFirma en tu sistema: no es una suposición documental, se ejecuta el binario de verdad.

## Interfaz gráfica PyQt6

La versión PyQt6 es la interfaz gráfica principal de este repositorio y está pensada para integrarse mejor visualmente en KDE Plasma. La lógica de negocio incluye instalación/actualización de AutoFirma, NSS, certificados, descarga oficial y verificación de huellas.

- `autofirma_gui_qt.py`: interfaz gráfica PyQt6.
- `instalar_autofirma_qt.sh`: lanzador principal. Comprueba e instala `python3-pyqt6` cuando es necesario.
- `autofirma.desktop`: acceso al menú de aplicaciones que abre el lanzador PyQt6.
- `instalar_autofirma.sh`: compatibilidad con instalaciones/comandos anteriores; simplemente redirige al lanzador PyQt6.

La antigua interfaz Tkinter (`autofirma_gui.py`) ya no forma parte del proyecto.

Uso:

```bash
chmod +x instalar_autofirma_qt.sh
./instalar_autofirma_qt.sh
```

Probada en máquina real: funciona correctamente.


## Robustez de la versión PyQt6

El lanzador comprueba que el intérprete Python sea 3.10 o superior antes de iniciar la aplicación. No se fija una versión concreta de PyQt6 en el repositorio: se utiliza el paquete `python3-pyqt6` proporcionado por Debian. Las versiones recientes de PyQt6 publicadas upstream requieren Python 3.10 o superior, y las versiones más nuevas pueden elevar ese mínimo; el paquete de Debian es quien determina la versión disponible en cada suite. citeturn2search0turn2search1

## PolicyKit en Debian actual

El proyecto comprueba `pkexec` y `polkitd` por separado. En Debian moderno, `policykit-1` es un paquete transitorio que depende de ambos, mientras que `pkexec` y `polkitd` son paquetes separados. Por ello el lanzador instala directamente `pkexec polkitd` cuando faltan. citeturn1search0turn1search11

Durante operaciones APT largas, la GUI muestra en tiempo real las líneas que devuelve `apt-get`. Si `apt-get update` falla durante la preparación de dependencias, se muestra una advertencia y se intenta continuar con los índices disponibles; si la instalación necesaria falla, la operación termina con el código y la salida disponibles para el usuario.
