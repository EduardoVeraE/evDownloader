# video-downloader

Descargador de cursos de video para **Platzi**, **Udemy** y **Codigofacilito**.
Diseño extensible por *extractores* (una plataforma = un extractor) y motor de
descarga **híbrido** sobre yt-dlp.

## Por qué existe

El proyecto se inspira en [`ivansaul/platzi-downloader`](https://github.com/ivansaul/platzi-downloader),
que dejó de funcionar cuando Platzi migró la entrega de video a **Mediastream
(`mdstrm.com`)**: la URL del playlist HLS ya no está en el HTML, se genera con
tokens. Se resuelve interceptando la petición real del reproductor (Playwright) y
manteniendo una **identidad coherente** entre navegación y descarga (evita los `403`).

## Plataformas soportadas

Cada plataforma se autentica de una de dos formas, según su anti-bot:

| Plataforma | Autenticación | Cómo |
|---|---|---|
| **Platzi** | Login manual asistido | `login` abre un navegador (Playwright) para que inicies sesión una vez; las cookies quedan guardadas |
| **Udemy** | Cookies del navegador real | Está tras Cloudflare Turnstile; se reutiliza la sesión de tu navegador con `--cookies-from-browser` |
| **Codigofacilito** | Cookies del navegador real | Video servido por BunnyCDN; se resuelve con yt-dlp y `--cookies-from-browser` |

> Para **Udemy** y **Codigofacilito** debes tener la sesión **ya iniciada en tu
> navegador** (Brave, Chrome, Safari, Edge o Firefox). No se usa el comando `login`.

## Requisitos

- **Python 3.14+** (lo gestiona `uv`)
- **FFmpeg** en el `PATH`
- **Chromium** de Playwright — solo si vas a usar **Platzi**

## Instalación rápida (macOS · Homebrew)

```bash
# 1. Dependencias del sistema
brew install uv ffmpeg

# 2. Dependencias del proyecto (uv instala Python 3.14 si falta)
uv sync

# 3. Solo si vas a usar Platzi: navegador de Playwright
uv run playwright install chromium
```

## Uso por plataforma

### Platzi

```bash
# Una vez: iniciar sesión (abre el navegador para login manual)
uv run video-downloader login

# Descargar el curso
uv run video-downloader download "https://platzi.com/cursos/<curso>/"
```

### Udemy

```bash
# Requiere estar logueado en el navegador (ej. Brave) y pasar --cookies-from-browser
uv run video-downloader download \
  "https://www.udemy.com/course/<curso>/" \
  --cookies-from-browser brave
```

### Codigofacilito

```bash
# Requiere estar logueado en el navegador (ej. Brave) y pasar --cookies-from-browser
uv run video-downloader download \
  "https://codigofacilito.com/cursos/<curso>" \
  --cookies-from-browser brave
```

La salida se organiza sola en `downloads/<Plataforma>/<curso>/<NN-módulo>/<NN-clase>.mp4`.

## Ejemplos

```bash
# Platzi, calidad máxima 1080p, en un directorio concreto
uv run video-downloader download "https://platzi.com/cursos/git-github/" \
  -q 1080 -o ~/Cursos

# Codigofacilito, solo las primeras 5 clases (prueba rápida), leyendo cookies de Chrome
uv run video-downloader download "https://codigofacilito.com/cursos/git-profesional" \
  --cookies-from-browser chrome --limit 5

# Udemy, sin descargar recursos/adjuntos y forzando re-descarga
uv run video-downloader download "https://www.udemy.com/course/<curso>/" \
  --cookies-from-browser brave --no-resources --overwrite

# Solo subtítulos en español e inglés (plataformas que delegan subs en yt-dlp)
uv run video-downloader download "<url>" --cookies-from-browser brave --sub-langs es,en

# Ver el navegador durante el login/descarga de Platzi (depuración)
uv run video-downloader download "https://platzi.com/cursos/<curso>/" --show-browser
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
uv run video-downloader status            # ¿hay sesión activa? (Platzi)
uv run video-downloader logout            # cerrar sesión guardada
uv run video-downloader clear-cache       # borrar la caché de estructura de cursos
```

## Arquitectura

| Capa | Módulo | Responsabilidad |
|---|---|---|
| CLI | `cli.py` | Comandos `login`, `logout`, `download`, `status`, `clear-cache` |
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
