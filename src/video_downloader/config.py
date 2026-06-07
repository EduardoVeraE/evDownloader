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
SESSION_FILE = DATA_DIR / "session.json"
CACHE_DIR = DATA_DIR / "cache"

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

# Tiempo máximo (segundos) para que el usuario inicie sesión manualmente.
LOGIN_TIMEOUT_S = 180


@dataclass(slots=True)
class Settings:
    """Ajustes de una ejecución concreta de descarga."""

    download_dir: Path = field(default_factory=lambda: Path.cwd() / "Courses")
    quality: str | None = None  # "1080", "720"... None = máxima disponible
    overwrite: bool = False
    downloader: str = "ytdlp"  # "ytdlp" (por defecto) | "native"
    headless: bool = True
    concurrency: int = 8
    limit: int | None = None  # nº máximo de clases de video a descargar (None = todas)
    resources: bool = True  # descargar resumen, archivos adjuntos, enlaces y MHTML


def ensure_dirs() -> None:
    """Crea los directorios persistentes si no existen."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
