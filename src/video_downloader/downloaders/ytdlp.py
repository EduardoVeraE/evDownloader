"""Motor de descarga por defecto: yt-dlp como librería.

yt-dlp ya incluye el extractor de Mediastream (``mdstrm.com/embed``), por lo que
resuelve los tokens (access_token, uid, sid, pid), los playlists anidados, la
encriptación AES-128, la selección de calidad y el muxeo con FFmpeg. Le pasamos
la fuente con headers/cookies coherentes para evitar los bloqueos 403.

Las cookies se entregan vía **cookiefile** en formato Netscape (no como header
``Cookie``): yt-dlp marca el header como deprecado y, además, el cookiefile le
permite enviar a cada host (Platzi, Mediastream) solo las cookies que le
corresponden por dominio.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from ..config import Settings
from ..models import Cookie, VideoSource
from .base import Downloader

# Cookies de sesión (sin expiración fija) escritas con esta expiración lejana
# para que yt-dlp no las descarte al cargar el cookiefile.
_SESSION_COOKIE_EXPIRY = 2147483647  # 2038-01-19, máx. de 32 bits


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

        if source.write_subs:
            # yt-dlp extrae los subtítulos junto al video (Udemy). Se escriben
            # como <nombre>.<lang>.vtt junto al .mp4, con el mismo outtmpl.
            opts["writesubtitles"] = True
            opts["subtitleslangs"] = [x for x in settings.sub_langs.split(",") if x]
            opts["subtitlesformat"] = "vtt/best"

        cookiefile: str | None = None
        try:
            if source.cookie_jar:
                cookiefile = self._write_cookiefile(source.cookie_jar)
                opts["cookiefile"] = cookiefile
            elif settings.cookies_from_browser:
                # yt-dlp lee las cookies directamente del navegador real del
                # usuario (Udemy: evita login/cookiefile y pasa Cloudflare).
                opts["cookiesfrombrowser"] = (settings.cookies_from_browser, None, None, None)
            elif source.cookies:
                # Respaldo si no hay cookies completas: header (deprecado).
                opts["http_headers"] = {
                    **source.http_headers,
                    "Cookie": "; ".join(f"{k}={v}" for k, v in source.cookies.items()),
                }

            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([source.url])
        finally:
            if cookiefile and os.path.exists(cookiefile):
                os.remove(cookiefile)

        final = dest.with_suffix(".mp4")
        if final.exists():
            return final
        # Si el contenedor difiere, devolver el primer archivo que coincida.
        matches = sorted(dest.parent.glob(dest.stem + ".*"))
        return matches[0] if matches else final

    @classmethod
    def _write_cookiefile(cls, jar: list[Cookie]) -> str:
        """Escribe las cookies a un cookiefile Netscape temporal y devuelve su ruta."""
        fd, path = tempfile.mkstemp(prefix="vd-cookies-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(render_netscape(jar))
        return path


def render_netscape(jar: list[Cookie]) -> str:
    """Serializa cookies al formato Netscape que consume yt-dlp/curl."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in jar:
        if not c.domain:
            continue
        include_subdomains = "TRUE" if c.domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.secure else "FALSE"
        expiry = int(c.expires) if c.expires and c.expires > 0 else _SESSION_COOKIE_EXPIRY
        lines.append(
            "\t".join(
                [c.domain, include_subdomains, c.path or "/", secure, str(expiry), c.name, c.value]
            )
        )
    return "\n".join(lines) + "\n"
