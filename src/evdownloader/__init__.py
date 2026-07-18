"""evDownloader: descargador de cursos de video para Platzi, Udemy y Codigofacilito."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("evDownloader")
except PackageNotFoundError:
    __version__ = "unknown"
