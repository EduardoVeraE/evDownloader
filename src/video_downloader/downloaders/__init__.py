"""Motores de descarga.

``ytdlp`` es el motor por defecto (delega a yt-dlp, que ya resuelve Mediastream);
``native`` es el respaldo configurable basado en rnet + FFmpeg.
"""

from __future__ import annotations

from .base import Downloader
from .native import NativeDownloader
from .ytdlp import YtDlpDownloader

_REGISTRY: dict[str, type[Downloader]] = {
    "ytdlp": YtDlpDownloader,
    "native": NativeDownloader,
}


def get_downloader(name: str) -> Downloader:
    """Devuelve una instancia del downloader por nombre (``ytdlp`` | ``native``)."""
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(f"Downloader desconocido: {name!r}") from None


__all__ = ["Downloader", "NativeDownloader", "YtDlpDownloader", "get_downloader"]
