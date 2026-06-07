"""Motor de descarga nativo (respaldo): rnet + FFmpeg.

Pensado como alternativa cuando yt-dlp falle. Resuelve la URL del master
``.m3u8`` (si la fuente es un embed de Mediastream, descarga la página del embed
con ``rnet`` —impersonando un navegador— y extrae el playlist) y delega a FFmpeg
la descarga de segmentos y el muxeo, pasándole headers/cookies coherentes para
evitar bloqueos 403. FFmpeg maneja de forma nativa HLS y AES-128.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ..config import RNET_IMPERSONATE, Settings
from ..models import VideoSource
from .base import Downloader

_M3U8_RE = re.compile(r"https?://[^\s\"'}\\]+?(?<!\.vtt)\.m3u8[^\s\"'}\\]*")


class NativeDownloader(Downloader):
    name = "native"

    async def download(self, source: VideoSource, dest: Path, settings: Settings) -> Path:
        m3u8_url = await self._resolve_m3u8(source)
        if not m3u8_url:
            raise RuntimeError(
                "El downloader nativo no pudo resolver un .m3u8 desde la fuente. "
                "Prueba con el motor por defecto (yt-dlp)."
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        out = dest.with_suffix(".mp4")
        await self._ffmpeg(m3u8_url, out, source, settings)
        return out

    async def _resolve_m3u8(self, source: VideoSource) -> str | None:
        if not source.is_embed:
            return source.url
        # Descargar la página del embed con identidad de navegador y extraer el m3u8.
        import rnet

        client = rnet.Client(impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None))
        headers = dict(source.http_headers)
        if source.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in source.cookies.items())
        resp = await client.get(source.url, headers=headers)
        text = await resp.text()
        match = _M3U8_RE.search(text)
        return match.group(0) if match else None

    async def _ffmpeg(
        self, m3u8_url: str, out: Path, source: VideoSource, settings: Settings
    ) -> None:
        header_lines = []
        for k, v in source.http_headers.items():
            if k.lower() == "user-agent":
                continue  # se pasa por -user_agent
            header_lines.append(f"{k}: {v}")
        if source.cookies:
            cookie = "; ".join(f"{k}={v}" for k, v in source.cookies.items())
            header_lines.append(f"Cookie: {cookie}")

        ua = source.http_headers.get("User-Agent", "")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        if ua:
            cmd += ["-user_agent", ua]
        if header_lines:
            cmd += ["-headers", "\r\n".join(header_lines) + "\r\n"]
        cmd += [
            "-i",
            m3u8_url,
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
        ]
        if settings.overwrite:
            cmd.append("-y")
        else:
            cmd.append("-n")
        cmd.append(str(out))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode not in (0, None):
            raise RuntimeError(
                f"FFmpeg falló (código {proc.returncode}): {stderr.decode(errors='ignore')[-500:]}"
            )
