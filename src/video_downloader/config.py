"""Configuración global: rutas, constantes y ajustes de ejecución.

Las rutas se resuelven con ``platformdirs`` para ser multiplataforma. La sesión
(cookies) y la caché viven en el directorio de datos del usuario; las descargas,
por defecto, en ``./Courses`` dentro del directorio de trabajo actual.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "video-downloader"

# --- Rutas persistentes -----------------------------------------------------
DATA_DIR = Path(user_data_dir(APP_NAME, appauthor=False))
CACHE_DIR = DATA_DIR / "cache"


def session_file(platform: str) -> Path:
    """Archivo de cookies para una plataforma (``session-{platform}.json``).

    Cada plataforma persiste su sesión por separado para que autenticarse en
    una (p. ej. Udemy) no pise la sesión de otra (Platzi).
    """
    return DATA_DIR / f"session-{platform}.json"

# --- Identidad de navegador (coherente entre navegación y descarga) ---------
# Usar el MISMO User-Agent al navegar con Playwright y al descargar evita los
# bloqueos 403 que sufría el proyecto original (navegaba como Chrome pero
# descargaba como Firefox).
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)

# rnet usa nombres de impersonación en PascalCase (p. ej. ``Chrome137``);
# mantenerlo alineado con el UA de arriba para coherencia de identidad.
RNET_IMPERSONATE = "Chrome137"

# --- Endpoints / dominios de Platzi y Mediastream ---------------------------
PLATZI_BASE_URL = "https://platzi.com"
LOGIN_URL = "https://platzi.com/login/"
# Endpoint para validar que la sesión sigue activa.
LOGIN_DETAILS_URL = "https://platzi.com/api/v1/auth/me/"
MEDIASTREAM_HOSTS = ("mdstrm.com",)

# --- Endpoints / dominios de Udemy ------------------------------------------
UDEMY_BASE_URL = "https://www.udemy.com"
UDEMY_LOGIN_URL = "https://www.udemy.com/join/login-popup/"

# --- Endpoints / dominios de Codigofacilito ---------------------------------
CODIGOFACILITO_BASE_URL = "https://codigofacilito.com"
CODIGOFACILITO_LOGIN_URL = "https://codigofacilito.com/login"

# Tiempo máximo (segundos) para que el usuario inicie sesión manualmente.
LOGIN_TIMEOUT_S = 180


@dataclass(slots=True)
class Settings:
    """Ajustes de una ejecución concreta de descarga."""

    download_dir: Path = field(default_factory=lambda: Path.cwd() / "downloads")
    quality: str | None = None  # "1080", "720"... None = máxima disponible
    overwrite: bool = False
    downloader: str = "ytdlp"  # "ytdlp" (por defecto) | "native"
    headless: bool = True
    concurrency: int = 8
    limit: int | None = None  # nº máximo de clases de video a descargar (None = todas)
    resources: bool = True  # descargar resumen, archivos adjuntos, enlaces y MHTML
    # Navegador del que yt-dlp lee las cookies (chrome, brave, safari...). Lo
    # usan los extractores que delegan en yt-dlp (Udemy) para autenticar sin
    # navegador automatizado. None = no usar cookies del navegador.
    cookies_from_browser: str | None = None
    # Idiomas de subtítulos a descargar (formato yt-dlp: "all", "es,en", "es.*").
    # Aplica a los extractores que delegan los subtítulos en yt-dlp (Udemy).
    sub_langs: str = "all"


def ensure_dirs() -> None:
    """Crea los directorios persistentes si no existen."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
