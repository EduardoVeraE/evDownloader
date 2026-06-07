"""Motor de descarga por defecto: yt-dlp como librería.

yt-dlp ya incluye el extractor de Mediastream (``mdstrm.com/embed``), por lo que
resuelve los tokens (access_token, uid, sid, pid), los playlists anidados, la
encriptación AES-128, la selección de calidad y el muxeo con FFmpeg. Le pasamos
la fuente con headers/cookies coherentes para evitar los bloqueos 403.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import Settings
from ..models import VideoSource
from .base import Downloader


class YtDlpDownloader(Downloader):
    name = "ytdlp"

    async def download(self, source: VideoSource, dest: Path, settings: Settings) -> Path:
        # yt-dlp es síncrono; ejecutarlo en un hilo para no bloquear el loop.
        return await asyncio.to_thread(self._run, source, dest, settings)

    def _run(self, source: VideoSource, dest: Path, settings: Settings) -> Path:
        import yt_dlp

        dest.parent.mkdir(parents=True, exist_ok=True)
        outtmpl = str(dest.with_suffix("")) + ".%(ext)s"

        opts: dict = {
            "outtmpl": outtmpl,
            "format": self._format_selector(settings.quality),
            "merge_output_format": "mp4",
            "http_headers": source.http_headers,
            "concurrent_fragment_downloads": settings.concurrency,
            "retries": 5,
            "fragment_retries": 5,
            "overwrites": settings.overwrite,
            "quiet": True,
            "no_warnings": True,
            "noprogress": False,
        }
        if source.cookies:
            opts["http_headers"] = {
                **source.http_headers,
                "Cookie": "; ".join(f"{k}={v}" for k, v in source.cookies.items()),
            }

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([source.url])

        final = dest.with_suffix(".mp4")
        if final.exists():
            return final
        # Si el contenedor difiere, devolver el primer archivo que coincida.
        matches = sorted(dest.parent.glob(dest.stem + ".*"))
        return matches[0] if matches else final
