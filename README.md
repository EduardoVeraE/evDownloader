# evDownloader

Descargador de cursos de video para **Platzi**, **Udemy** y **Codigofacilito**.
Diseño extensible por *extractores* (una plataforma = un extractor) y motor de
descarga **híbrido** sobre yt-dlp.

## Plataformas soportadas

Cada plataforma se autentica de una de dos formas, según su anti-bot:

| Plataforma | Autenticación | Cómo |
|---|---|---|
| **Platzi** | Login manual asistido | `login` abre un navegador (Playwright) para que inicies sesión una vez; las cookies quedan guardadas |
| **Udemy** | Cookies del navegador real | Está tras Cloudflare Turnstile; se reutiliza la sesión de tu navegador con `--cookies-from-browser` |
| **Codigofacilito** | Cookies del navegador real | Video servido por BunnyCDN; se resuelve con yt-dlp y `--cookies-from-browser` |

> Para **Udemy** y **Codigofacilito** debes tener la sesión **ya iniciada en tu
> navegador** (Brave, Chrome, Safari, Edge o Firefox). No se usa el comando `login`.

## Instalación

evDownloader es una CLI de Python. Instálala como herramienta aislada con **uv**
(recomendado) o **pipx**:

```bash
# Con uv
uv tool install evdownloader

# …o con pipx
pipx install evdownloader
```

Esto deja disponibles los comandos `evdownloader` y su alias corto `evd`.

### Prerrequisitos

- **FFmpeg** en el `PATH` (muxeo y HLS):

  ```bash
  brew install ffmpeg      # macOS / Linux (Homebrew)
  scoop install ffmpeg     # Windows (Scoop)
  sudo apt install ffmpeg  # Debian / Ubuntu
  ```

- **Chromium de Playwright** — *solo si vas a usar Platzi*. Instálalo una vez:

  ```bash
  evdownloader setup
  ```

  Udemy y Codigofacilito no lo necesitan.

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

### Codigofacilito

```bash
# Requiere estar logueado en el navegador (ej. Brave) y pasar --cookies-from-browser
evdownloader download \
  "https://codigofacilito.com/cursos/<curso>" \
  --cookies-from-browser brave
```

La salida se organiza sola en `downloads/<Plataforma>/<curso>/<NN-módulo>/<NN-clase>.mp4`.

## Ejemplos

```bash
# Platzi, calidad máxima 1080p, en un directorio concreto
evdownloader download "https://platzi.com/cursos/git-github/" -q 1080 -o ~/Cursos

# Codigofacilito, solo las primeras 5 clases (prueba rápida), cookies de Chrome
evd download "https://codigofacilito.com/cursos/git-profesional" \
  --cookies-from-browser chrome --limit 5

# Udemy, sin descargar recursos/adjuntos y forzando re-descarga
evdownloader download "https://www.udemy.com/course/<curso>/" \
  --cookies-from-browser brave --no-resources --overwrite

# Solo subtítulos en español e inglés (plataformas que delegan subs en yt-dlp)
evdownloader download "<url>" --cookies-from-browser brave --sub-langs es,en

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
| `--cookies-from-browser` | Navegador del que leer cookies (`brave`, `chrome`, `safari`…). **Requerido para Udemy y Codigofacilito** |
| `--sub-langs` | Idiomas de subtítulos (yt-dlp): `all`, `es,en`, `es.*`… |
| `--show-browser` | Mostrar el navegador (no headless) — solo Platzi |

### Otros comandos

```bash
evdownloader setup          # instala Chromium de Playwright (solo Platzi)
evdownloader status         # ¿hay sesión activa? (Platzi)
evdownloader logout         # cerrar sesión guardada
evdownloader clear-cache    # borrar la caché de estructura de cursos
```

## Desarrollo

```bash
git clone https://github.com/EduardoVeraE/evDownloader
cd evDownloader
uv sync --extra dev
uv run playwright install chromium   # solo si vas a probar Platzi

# Calidad
uv run ruff check src/ tests/
uv run mypy src/evdownloader
uv run python -m pytest
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

## Notas de compatibilidad (Python 3.14)

`yt-dlp`, `pydantic` (>=2.12), `rnet` (>=2.4.2) y `greenlet` (>=3.5.1) publican
wheels para 3.14. `playwright` 1.60 aún no lo clasifica oficialmente pero funciona
en la práctica (su única dependencia C, `greenlet`, ya lo soporta). Si surgiera un
problema, el plan de respaldo es migrar el navegado a `nodriver`.

## Aviso legal

Úsalo solo para descargar contenido al que tengas acceso legítimo (tu propia
suscripción), respetando los Términos de Servicio de cada plataforma.
