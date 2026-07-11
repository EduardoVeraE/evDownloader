"""Interfaz común para los motores de descarga."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import Settings
from ..models import VideoSource


class Downloader(ABC):
    """Contrato de un motor de descarga.

    ``download`` recibe la fuente ya resuelta (URL + headers + cookies
    coherentes) y la ruta de destino *sin extensión*; el motor decide el
    contenedor final (normalmente ``.mp4``) y devuelve la ruta escrita.
    """

    name: str = "base"

    @abstractmethod
    async def download(
        self, source: VideoSource, dest: Path, settings: Settings
    ) -> Path:
        """Descarga el video a ``dest`` y devuelve la ruta del archivo final."""

    @staticmethod
    def _format_selector(quality: str | None) -> str:
        """Construye el selector de formato de yt-dlp según la calidad pedida."""
        if quality:
            h = quality.rstrip("p")
            return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
        return "bv*+ba/b"

    @staticmethod
    def _hls_format_selector(quality: str | None) -> str:
        """Selector de formato que solo elige formatos HLS ([protocol^=m3u8]).

        Útil como fallback cuando la descarga directa (progressive) falla,
        ya que los CDNs de HLS suelen ser más tolerantes a errores de red
        y expiración de tokens.
        """
        if quality:
            h = quality.rstrip("p")
            return (
                f"bv*[protocol^=m3u8][height<={h}]"
                f"+ba[protocol^=m3u8]/b[protocol^=m3u8]"
            )
        return "bv*[protocol^=m3u8]+ba[protocol^=m3u8]/b[protocol^=m3u8]"
