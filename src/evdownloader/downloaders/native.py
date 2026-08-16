"""Motor de descarga nativo (respaldo): rnet + FFmpeg.

Pensado como alternativa cuando yt-dlp falle. Resuelve la URL del master
``.m3u8`` (si la fuente es un embed de Mediastream, descarga la página del embed
con ``rnet`` —impersonando un navegador— y extrae el playlist) y delega a FFmpeg
la descarga de segmentos y el muxeo, pasándole headers/cookies coherentes para
evitar bloqueos 403. FFmpeg maneja de forma nativa HLS y AES-128.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from urllib.parse import urljoin

from .. import browser
from ..config import RNET_IMPERSONATE, Settings
from ..models import VideoSource
from ..url_policy import URLPolicy, is_credential_header, parse_url_authority
from .base import Downloader

_M3U8_RE = re.compile(r"https?://[^\s\"'}\\]+?(?<!\.vtt)\.m3u8[^\s\"'}\\]*")
_MAX_REDIRECTS = 5


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
        out = dest.parent / f"{dest.name}.mp4"
        await self._ffmpeg(m3u8_url, out, source, settings)
        return out

    async def _resolve_m3u8(self, source: VideoSource) -> str | None:
        policy = self._source_policy(source)
        if not policy.allows(source.url):
            raise RuntimeError("El downloader nativo rechazó una fuente no confiable.")
        if not source.is_embed:
            return source.url
        # Descargar la página del embed con identidad de navegador y extraer el m3u8.
        import rnet

        client = rnet.Client(impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None))
        current_url = source.url
        visited: set[str] = set()
        for hop in range(_MAX_REDIRECTS + 1):
            if not policy.allows(current_url) or current_url in visited:
                return None
            visited.add(current_url)
            headers = {
                key: value
                for key, value in source.http_headers.items()
                if not is_credential_header(key)
            }
            cookie = browser.cookie_header_for_url(
                source.cookie_jar,
                current_url,
                trusted_host_suffixes=source.trusted_host_suffixes,
            )
            if cookie:
                headers["Cookie"] = cookie
            resp = await client.get(current_url, headers=headers, allow_redirects=False)
            try:
                status = resp.status_code.as_int()
                if status in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location")
                    if not isinstance(location, str) or not location or hop == _MAX_REDIRECTS:
                        return None
                    current_url = urljoin(current_url, location)
                    continue
                if not 200 <= status < 300:
                    return None
                text = await resp.text()
            finally:
                with contextlib.suppress(Exception):
                    await resp.close()
            match = _M3U8_RE.search(text)
            return match.group(0) if match and policy.allows(match.group(0)) else None
        return None

    async def _ffmpeg(
        self, m3u8_url: str, out: Path, source: VideoSource, settings: Settings
    ) -> None:
        policy = self._source_policy(source)
        if not policy.allows(m3u8_url):
            raise RuntimeError("El downloader nativo rechazó un playlist no confiable.")
        header_lines = []
        for k, v in source.http_headers.items():
            if k.lower() == "user-agent" or is_credential_header(k):
                continue  # se pasa por -user_agent
            header_lines.append(f"{k}: {v}")
        cookie = browser.cookie_header_for_url(
            source.cookie_jar,
            m3u8_url,
            trusted_host_suffixes=source.trusted_host_suffixes,
        )
        if cookie:
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
        await proc.communicate()
        if proc.returncode not in (0, None):
            raise RuntimeError(f"FFmpeg falló (código {proc.returncode}).")

    @staticmethod
    def _source_policy(source: VideoSource) -> URLPolicy:
        authority = parse_url_authority(source.url)
        suffixes = source.trusted_host_suffixes or (
            (authority.hostname,) if authority is not None else ()
        )
        return URLPolicy("media", suffixes)
