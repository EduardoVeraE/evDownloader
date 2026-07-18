# evDownloader

Te ayuda a descargar cursos de **Platzi**, **Udemy** y **Codigofacilito** para verlos offline, debes tener acceso al curso en tu sesión.
Diseño extensible por *extractores* (una plataforma = un extractor) y motor de
descarga **híbrido** sobre yt-dlp.

## Plataformas soportadas

Cada plataforma se autentica de una de dos formas, según su anti-bot:

| Plataforma | Autenticación | Cómo |
|---|---|---|
| **Platzi** | Login manual asistido | `login` abre un navegador (Playwright) para que inicies sesión una vez; las cookies quedan guardadas |
| **Udemy** | Sesión persistida o cookies del navegador real | Está tras Cloudflare Turnstile; usa la sesión guardada por `login` o `--cookies-from-browser` |
| **Codigofacilito** | Cookies del navegador real | Video servido por BunnyCDN; se resuelve con yt-dlp y `--cookies-from-browser` |

> Para **Udemy** puedes usar una sesión guardada con `evd login udemy` o leer las
> cookies de un navegador real (Brave, Chrome, Safari, Edge o Firefox). Para
> Codigofacilito se recomienda `--cookies-from-browser`.

## Instalación

evDownloader es una CLI de Python. Instálala como herramienta aislada con **uv**
(recomendado) o **pipx**:

```bash
# Con uv
uv tool install evDownloader

# …o con pipx
pipx install evDownloader
```

Esto deja disponibles los comandos `evdownloader` y su alias corto `evd`.

Si estás trabajando desde el código fuente, puedes instalar la aplicación y
todas sus dependencias Python con `reqs.txt`:

```bash
# macOS / Linux
python3 -m pip install -r reqs.txt -e .

# Windows PowerShell
py -3.14 -m pip install -r reqs.txt -e .
```


### Prerrequisitos

Sigue estos pasos antes de usar `evdownloader`:

1. **Instala Python 3.14+ y un gestor de herramientas.** `uv` es la opción
   recomendada porque puede instalar Python automáticamente:

   ```bash
   # macOS / Linux
   curl -LsSf https://astral.sh/uv/install.sh | sh
   exec "$SHELL"
   uv python install 3.14
   uv python find 3.14
   ```

   En Windows, abre PowerShell y vuelve a abrirlo después de instalar `uv`:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   uv python install 3.14
   uv python find 3.14
   ```

   Si prefieres `pipx`, instala primero Python 3.14 y luego `pipx`:

   ```bash
   # macOS
   brew install python@3.14 pipx
   pipx ensurepath

   # Debian / Ubuntu
   sudo apt update
   sudo apt install -y python3.14 pipx
   pipx ensurepath
   ```

   En Windows puedes instalar Python desde
   `https://www.python.org/downloads/` y ejecutar en PowerShell:

   ```powershell
   py -3.14 -m pip install --user pipx
   py -3.14 -m pipx ensurepath
   ```

   Comprueba la versión antes de continuar:

   ```bash
   uv run --python 3.14 python --version  # instalación con uv
   python3 --version                      # instalación con pipx en macOS/Linux
   ```

   ```powershell
   py -3.14 --version                     # Windows
   ```

2. **Instala FFmpeg** y comprueba que esté en el `PATH` (muxeo y HLS):

   ```bash
   # macOS
   brew install ffmpeg

   # Debian / Ubuntu
   sudo apt update
   sudo apt install -y ffmpeg

   # Windows (PowerShell con winget)
   winget install --id Gyan.FFmpeg.Shared -e

   # Windows (PowerShell con Scoop, alternativa)
   scoop install ffmpeg
   ```

   ```bash
   ffmpeg -version
   ```

3. **Solo para Udemy con DRM**, instala Bento4 para disponer de `mp4decrypt`.
   En macOS puedes instalarlo con Homebrew:

   ```bash
   brew install bento4
   mp4decrypt --version
   ```

   En Linux o Windows, descarga Bento4 desde
   `https://www.bento4.com/downloads/`, descomprime el paquete y agrega su
   directorio `bin` al `PATH`. Por ejemplo:

   ```bash
   # Linux/macOS, ajusta la ruta a la versión descargada
   export PATH="$HOME/Bento4-SDK/bin:$PATH"
   mp4decrypt --version
   ```

   ```powershell
   # Windows PowerShell, ajusta la ruta a la versión descargada
   $env:Path += ";C:\Bento4\bin"
   mp4decrypt.exe --version
   ```

   El archivo `.wvd` es un dispositivo Widevine sensible. Usa únicamente un
   dispositivo autorizado y no lo subas al repositorio.

4. **Solo si vas a usar Platzi**, instala Chromium de Playwright una vez:

   ```bash
   evdownloader setup
   ```

   Udemy y Codigofacilito no necesitan Chromium de Playwright.

## Uso por plataforma

### Platzi

```bash
# Una vez: iniciar sesión (abre el navegador para login manual)
evdownloader login

# Descargar el curso
evdownloader download "https://platzi.com/cursos/<curso>/"
```

### Udemy

```bash
# Requiere estar logueado en el navegador (ej. Brave) y pasar --cookies-from-browser
evdownloader download \
  "https://www.udemy.com/course/<curso>/" \
  --cookies-from-browser brave
```

#### Udemy con DRM

Si el archivo está en `vendors/` y ejecutas el
comando desde la raíz del proyecto:

```bash
evd download \
  "https://www.udemy.com/course/<curso>/" \
  --cookies-from-browser brave \
  --use-drm \
  --drm-device "$PWD/vendors/udemy_l3.wvd" \
  --output ./downloads
```

`--drm-device` acepta cualquier ruta absoluta o relativa a un `.wvd` válido.
`--use-drm` es obligatorio para activar el descifrado; si falta el dispositivo o
`mp4decrypt`, la ejecución termina con un error explícito. El resultado final es
el MP4 desencriptado dentro de `downloads/Udemy/<curso>/...`.

Limitación conocida: el flujo DRM actual puede no generar archivos VTT/SRT, esta
en curso la investigación.

### Codigofacilito

```bash
# Requiere estar logueado en el navegador (ej. Brave) y pasar --cookies-from-browser
evdownloader download \
  "https://codigofacilito.com/cursos/<curso>" \
  --cookies-from-browser brave
```

La salida se organiza sola en `downloads/<Plataforma>/<curso>/<NN-módulo>/<NN-clase>.mp4`.

## Ejemplos
 Usa el comando `evdownload` o su alias `evd`

```bash
# Platzi, calidad máxima 1080p, en un directorio concreto
evdownloader download "https://platzi.com/cursos/git-github/" -q 1080 -o ~/Cursos

# Codigofacilito, solo las primeras 5 clases (prueba rápida), cookies de Chrome
evd download "https://codigofacilito.com/cursos/git-profesional" \
  --cookies-from-browser chrome --limit 5

# Udemy, sin descargar recursos/adjuntos y forzando re-descarga
evd download "https://www.udemy.com/course/<curso>/" \
  --cookies-from-browser brave --no-resources --overwrite

# Udemy protegido por DRM, con el dispositivo Widevine del proyecto
evd download "https://www.udemy.com/course/<curso>/" \
  --cookies-from-browser brave --use-drm \
  --drm-device "$PWD/vendors/udemy_l3.wvd"

# Solo subtítulos en español e inglés (plataformas que delegan subs en yt-dlp)
evd download "<url>" --cookies-from-browser brave --sub-langs es,en

# Ver el navegador durante el login/descarga de Platzi (depuración)
evdownloader download "https://platzi.com/cursos/<curso>/" --show-browser
```

## Opciones del comando `download`

| Opción | Descripción |
|---|---|
| `-q`, `--quality` | Calidad máxima: `1080`, `720`… (por defecto: la máxima disponible) |
| `-o`, `--output` | Directorio de salida (por defecto `./downloads`) |
| `-d`, `--downloader` | Motor: `ytdlp` (por defecto) o `native` (rnet + FFmpeg) |
| `-w`, `--overwrite` | Sobrescribir archivos existentes |
| `-n`, `--limit` | Descargar solo las primeras N clases de video |
| `--no-cache` | Ignorar la caché de estructura del curso |
| `--no-resources` | No descargar resumen, adjuntos, enlaces ni MHTML |
| `--cookies-from-browser` | Navegador del que leer cookies (`brave`, `chrome`, `safari`…). Fallback para Udemy; requerido si no hay sesión persistida |
| `--sub-langs` | Idiomas de subtítulos (yt-dlp): `all`, `es,en`, `es.*`… |
| `--show-browser` | Mostrar el navegador (no headless) — solo Platzi |
| `--use-drm` | Activar el flujo integrado de detección, descifrado y validación DRM |
| `--drm-device` | Ruta al dispositivo Widevine `.wvd`; requerido junto con `--use-drm` |
| `--drm-license-server` | Sobrescribir la URL del servidor de licencias; uso avanzado |
| `--drm-token` | Sobrescribir el token DRM; uso avanzado, no necesario normalmente |

### Otros comandos

```bash
evdownloader setup          # instala Chromium de Playwright (solo Platzi)
evdownloader status         # ¿hay sesión activa? (Platzi)
evdownloader logout         # cerrar sesión guardada
evdownloader clear-cache    # borrar la caché de estructura de cursos
```

## Arquitectura

| Capa | Módulo | Responsabilidad |
|---|---|---|
| CLI | `cli.py` | Comandos `login`, `logout`, `download`, `status`, `clear-cache`, `setup` |
| Sesión | `session.py`, `browser.py` | Login manual y cookies persistentes (Platzi) |
| Extractores | `extractors/` | Estructura del curso + resolución de video por plataforma |
| Descarga | `downloaders/` | `ytdlp` (por defecto) y `native` (rnet + FFmpeg) |
| Orquestación | `service.py` | Une todo y organiza la salida en carpetas |

Hay **dos patrones de extractor**: navegador (Platzi, intercepta la red con
Playwright) y delegación en yt-dlp + cookies del navegador (Udemy, Codigofacilito,
sin navegador automatizado). Añadir una plataforma es escribir un extractor nuevo;
el núcleo es agnóstico.

## Aviso legal

Úsalo solo para descargar contenido al que tengas acceso legítimo (tu propia
suscripción), respetando los Términos de Servicio de cada plataforma.
