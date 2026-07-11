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

from rich.console import Console

from ..config import Settings
from ..models import Cookie, VideoSource
from .base import Downloader

console = Console()

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

        base_opts: dict = {
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            # ``merge_output_format`` solo normaliza el contenedor cuando hay que
            # muxear video+audio separados. Un formato progresivo único conserva
            # la extensión que le asigna el extractor (BunnyCDN de Codigofacilito
            # etiqueta su "source" —un MP4 válido— como ``.json``). El postprocesador
            # ``FFmpegVideoRemuxer`` fuerza el contenedor final a mp4 sin recodificar;
            # si ya es mp4, yt-dlp lo omite (no afecta a Platzi/Udemy). Se declara
            # explícitamente porque la opción ``remux_video`` solo la traduce el CLI.
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
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
            base_opts["writesubtitles"] = True
            base_opts["subtitleslangs"] = [x for x in settings.sub_langs.split(",") if x]
            base_opts["subtitlesformat"] = "vtt/best"

        cookiefile: str | None = None
        try:
            if source.cookie_jar:
                cookiefile = self._write_cookiefile(source.cookie_jar)
                base_opts["cookiefile"] = cookiefile
            elif settings.cookies_from_browser:
                # yt-dlp lee las cookies directamente del navegador real del
                # usuario (Udemy: evita login/cookiefile y pasa Cloudflare).
                base_opts["cookiesfrombrowser"] = (settings.cookies_from_browser, None, None, None)
            elif source.cookies:
                # Respaldo si no hay cookies completas: header (deprecado).
                base_opts["http_headers"] = {
                    **source.http_headers,
                    "Cookie": "; ".join(f"{k}={v}" for k, v in source.cookies.items()),
                }

            # --- Cadena de formatos: progressive primero, HLS como fallback ---
            primary_fmt = self._format_selector(settings.quality)
            hls_fmt = self._hls_format_selector(settings.quality)

            formats_to_try: list[tuple[str, str]] = [(primary_fmt, "directa")]
            if hls_fmt != primary_fmt:
                formats_to_try.append((hls_fmt, "HLS"))

            last_error: Exception | None = None
            for fmt, label in formats_to_try:
                if label != "directa":
                    console.print(f"[yellow]  ↻ Fallback a descarga {label}...[/yellow]")
                try:
                    opts = {**base_opts, "format": fmt}
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([source.url])
                    last_error = None
                    break
                except yt_dlp.utils.DownloadError as exc:
                    last_error = exc
                    continue

            if last_error is not None:
                raise last_error
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
