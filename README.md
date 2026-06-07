# video-downloader

Descargador de cursos de video con **foco inicial en Platzi**. Diseño extensible
por *extractores* (para añadir otras plataformas) y motor de descarga **híbrido**.

## Por qué existe

El proyecto se inspira en [`ivansaul/platzi-downloader`](https://github.com/ivansaul/platzi-downloader),
que dejó de funcionar cuando Platzi migró la entrega de video a **Mediastream
(`mdstrm.com`)**. La URL del playlist HLS ya no está incrustada en el HTML: se
genera dinámicamente con tokens. Este proyecto resuelve eso con dos cambios clave:

1. **Interceptación de red** (Playwright) en lugar de regex sobre el HTML
   estático: se captura la petición real del reproductor (`mdstrm.com/embed/{id}`
   o el master `.m3u8`) junto con cookies, `User-Agent` y `Referer`.
2. **Identidad coherente** entre navegación y descarga (evita los `403`).

## Arquitectura

| Capa | Módulo | Responsabilidad |
|---|---|---|
| CLI | `cli.py` | Comandos `login`, `logout`, `download`, `status`, `clear-cache` |
| Sesión | `session.py`, `browser.py` | Login manual y cookies persistentes |
| Extractores | `extractors/` | Estructura del curso + resolución de video por plataforma |
| Descarga | `downloaders/` | `ytdlp` (por defecto) y `native` (rnet + FFmpeg) |
| Orquestación | `service.py` | Une todo y organiza la salida en carpetas |

## Requisitos

- **Python 3.14+**
- **FFmpeg** en el `PATH`
- Navegador de Playwright (Chromium)

## Instalación

```bash
uv sync
uv run playwright install chromium
```

## Uso

```bash
# 1. Iniciar sesión (abre el navegador para login manual)
uv run video-downloader login

# 2. Descargar un curso (calidad máxima por defecto)
uv run video-downloader download "https://platzi.com/cursos/<curso>/" -q 1080

# Motor de respaldo (rnet + FFmpeg)
uv run video-downloader download "<url>" --downloader native

# Otros
uv run video-downloader status
uv run video-downloader clear-cache
```

## Notas de compatibilidad (Python 3.14)

`yt-dlp`, `pydantic` (>=2.12), `rnet` (>=2.4.2) y `greenlet` (>=3.5.1) publican
wheels para 3.14. `playwright` 1.60 aún no lo clasifica oficialmente pero funciona
en la práctica (su única dependencia C, `greenlet`, ya lo soporta). Si surgiera un
problema, el plan de respaldo es migrar el navegado a `nodriver`.

## Aviso legal

Úsalo solo para descargar contenido al que tengas acceso legítimo (tu propia
suscripción), respetando los Términos de Servicio de cada plataforma.
