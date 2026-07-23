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
import contextlib
import os
import stat
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

from rich.console import Console

from ..config import Settings
from ..models import Cookie, DrmInfo, DrmRefresher, VideoSource
from .base import Downloader

console = Console()

# Cookies de sesión (sin expiración fija) escritas con esta expiración lejana
# para que yt-dlp no las descarte al cargar el cookiefile.
_SESSION_COOKIE_EXPIRY = 2147483647  # 2038-01-19, máx. de 32 bits
_DRM_ARTIFACT_EXTENSIONS = frozenset({".mp4", ".m4a", ".isma", ".ismv"})
_MEDIA_EXTENSIONS = frozenset({".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".ts", ".webm"})
_MAX_SUBTITLE_BYTES = 10 * 1024 * 1024
_ASCII_WHITESPACE = b" \t\r\n"


async def _run_drm_refresher(refresher: DrmRefresher) -> DrmInfo | None:
    return await refresher()


class YtDlpDownloader(Downloader):
    name = "ytdlp"
    supports_managed_subtitles = True

    async def download(self, source: VideoSource, dest: Path, settings: Settings) -> Path:
        # yt-dlp es síncrono; ejecutarlo en un hilo para no bloquear el loop.
        return await asyncio.to_thread(self._run, source, dest, settings)

    async def download_subtitles(
        self, source: VideoSource, dest: Path, settings: Settings
    ) -> list[Path]:
        """Descarga solo subtítulos y devuelve ``dest.*.vtt`` en orden de ruta."""
        return await asyncio.to_thread(self._run_subtitles, source, dest, settings)

    @staticmethod
    def _common_options(source: VideoSource, dest: Path, settings: Settings) -> dict:
        return {
            "outtmpl": str(dest) + ".%(ext)s",
            "http_headers": source.http_headers,
            "concurrent_fragment_downloads": settings.concurrency,
            "retries": 5,
            "fragment_retries": 5,
            "overwrites": settings.overwrite,
            "quiet": True,
            "no_warnings": True,
            "noprogress": False,
        }

    @contextlib.contextmanager
    def _cookie_options(
        self, base_opts: dict, source: VideoSource, settings: Settings
    ) -> Iterator[dict]:
        opts = dict(base_opts)
        cookiefile: str | None = None
        try:
            if source.cookie_jar:
                cookiefile = self._write_cookiefile(source.cookie_jar)
                opts["cookiefile"] = cookiefile
            elif settings.cookies_from_browser:
                # yt-dlp lee las cookies directamente del navegador real del
                # usuario (Udemy: evita login/cookiefile y pasa Cloudflare).
                opts["cookiesfrombrowser"] = (
                    settings.cookies_from_browser,
                    None,
                    None,
                    None,
                )
            elif source.cookies:
                # Respaldo si no hay cookies completas: header (deprecado).
                opts["http_headers"] = {
                    **source.http_headers,
                    "Cookie": "; ".join(f"{k}={v}" for k, v in source.cookies.items()),
                }
            yield opts
        finally:
            if cookiefile and os.path.exists(cookiefile):
                os.remove(cookiefile)

    def _run(self, source: VideoSource, dest: Path, settings: Settings) -> Path:
        import yt_dlp

        dest.parent.mkdir(parents=True, exist_ok=True)

        # DRM path: download encrypted media, then decrypt with mp4decrypt.
        if source.drm and settings.use_drm:
            return self._run_drm(source, dest, settings)
        if source.drm and not settings.use_drm:
            raise RuntimeError(
                "DRM content detected but --use-drm is disabled. "
                "Enable --use-drm to decrypt DRM-protected content, or "
                "use the 'drm-proof' command for manual decryption."
            )

        base_opts: dict = {
            **self._common_options(source, dest, settings),
            "merge_output_format": "mp4",
            # ``merge_output_format`` solo normaliza el contenedor cuando hay que
            # muxear video+audio separados. Un formato progresivo único conserva
            # la extensión que le asigna el extractor (BunnyCDN de Codigofacilito
            # etiqueta su "source" —un MP4 válido— como ``.json``). El postprocesador
            # ``FFmpegVideoRemuxer`` fuerza el contenedor final a mp4 sin recodificar;
            # si ya es mp4, yt-dlp lo omite (no afecta a Platzi/Udemy). Se declara
            # explícitamente porque la opción ``remux_video`` solo la traduce el CLI.
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        }

        if source.write_subs:
            # yt-dlp extrae los subtítulos junto al video (Udemy). Se escriben
            # como <nombre>.<lang>.vtt junto al .mp4, con el mismo outtmpl.
            base_opts["writesubtitles"] = True
            base_opts["subtitleslangs"] = [x for x in settings.sub_langs.split(",") if x]
            base_opts["subtitlesformat"] = "vtt/best"

        with self._cookie_options(base_opts, source, settings) as base_opts:
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

        final = dest.parent / f"{dest.name}.mp4"
        if self._is_regular_file(final):
            return final
        matches = sorted(
            path
            for path in dest.parent.iterdir()
            if (
                path.name.startswith(dest.name + ".")
                and path.suffix.lower() in _MEDIA_EXTENSIONS
                and self._is_regular_file(path)
            )
        )
        if matches:
            return matches[0]
        raise RuntimeError("yt-dlp produced no supported media file")

    def _run_subtitles(self, source: VideoSource, dest: Path, settings: Settings) -> list[Path]:
        import yt_dlp

        if not source.write_subs:
            raise ValueError("Downloader-managed subtitle recovery requires source.write_subs=True")

        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{dest.name}.subtitles-", dir=dest.parent
        ) as staging_dir:
            staged_base = Path(staging_dir) / dest.name
            opts = {
                **self._common_options(source, staged_base, settings),
                "overwrites": True,
                "skip_download": True,
                "writesubtitles": True,
                "subtitleslangs": [x for x in settings.sub_langs.split(",") if x],
                "subtitlesformat": "vtt/best",
            }

            with (
                self._cookie_options(opts, source, settings) as opts,
                yt_dlp.YoutubeDL(opts) as ydl,
            ):
                ydl.download([source.url])

            staged = sorted(
                path
                for path in Path(staging_dir).iterdir()
                if path.name.startswith(staged_base.name + ".") and path.suffix == ".vtt"
            )
            if any(not self._valid_webvtt(path) for path in staged):
                raise RuntimeError("Managed subtitle recovery produced invalid WebVTT output")

            published: list[Path] = []
            for path in staged:
                final_path = dest.parent / path.name
                os.replace(path, final_path)
                published.append(final_path)
            return published

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        try:
            return stat.S_ISREG(path.lstat().st_mode)
        except OSError:
            return False

    @classmethod
    def _valid_webvtt(cls, path: Path) -> bool:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SUBTITLE_BYTES:
                return False
            with path.open("rb") as subtitle_file:
                content = subtitle_file.read(_MAX_SUBTITLE_BYTES + 1)
            if len(content) > _MAX_SUBTITLE_BYTES:
                return False
            content.decode("utf-8")
        except OSError, UnicodeDecodeError:
            return False

        if content.startswith(b"\xef\xbb\xbf"):
            content = content[3:]
        content = content.lstrip(_ASCII_WHITESPACE)
        return content == b"WEBVTT" or (
            content.startswith(b"WEBVTT") and content[6:7] in _ASCII_WHITESPACE
        )

    def _run_drm(self, source: VideoSource, dest: Path, settings: Settings) -> Path:
        """Download encrypted media and decrypt via prove_decrypt_path."""
        import yt_dlp

        from ..drm import normalize_widevine_license_input, prove_decrypt_path
        from ..drm.license import post_license_challenge

        if not settings.drm_device:
            raise RuntimeError(
                "DRM decryption requires a .wvd device file. "
                "Provide one via --drm-device /path/to/device.wvd"
            )
        device_path = Path(settings.drm_device)
        if not device_path.is_file():
            raise RuntimeError(
                f"Device file not found: {device_path}. Provide a valid .wvd file via --drm-device."
            )

        # Build Cookie header from source cookies for the license POST.
        extra_headers: dict[str, str] = {}
        if source.cookie_jar:
            cookie_str = "; ".join(f"{c.name}={c.value}" for c in source.cookie_jar)
            extra_headers["Cookie"] = cookie_str
        elif source.cookies:
            extra_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in source.cookies.items())
        extra_headers.setdefault("Referer", "https://www.udemy.com/")

        # Isolate each attempt so a retry never consumes a previous attempt's files.
        staging_id = uuid.uuid4().hex
        outtmpl = str(dest) + f".encrypted.{staging_id}.%(ext)s"

        cookiefile: str | None = None
        try:
            base_opts: dict = {
                "outtmpl": outtmpl,
                "merge_output_format": "mp4",
                "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
                "http_headers": source.http_headers,
                "concurrent_fragment_downloads": settings.concurrency,
                "retries": 5,
                "fragment_retries": 5,
                "overwrites": settings.overwrite,
                "quiet": True,
                "no_warnings": True,
                "noprogress": False,
                # Allow downloading unplayable (DRM-protected) formats.
                "allow_unplayable_formats": True,
                "format": "bv*+ba/b",
            }

            if source.cookie_jar:
                cookiefile = self._write_cookiefile(source.cookie_jar)
                base_opts["cookiefile"] = cookiefile
            elif settings.cookies_from_browser:
                base_opts["cookiesfrombrowser"] = (settings.cookies_from_browser, None, None, None)
            elif source.cookies:
                base_opts["http_headers"] = {
                    **source.http_headers,
                    "Cookie": "; ".join(f"{k}={v}" for k, v in source.cookies.items()),
                }

            with yt_dlp.YoutubeDL(base_opts) as ydl:
                ydl.download([source.url])
        finally:
            if cookiefile and os.path.exists(cookiefile):
                os.remove(cookiefile)

        encrypted_paths = self._encrypted_artifacts(dest, staging_id)
        if not encrypted_paths:
            raise RuntimeError(
                "DRM download produced no compatible encrypted file. "
                "The media source may be unavailable."
            )

        # Refresh only when the source provides a safe mechanism and no explicit
        # token was supplied. This runs after media download and before challenge.
        refresher = source.drm_refresher
        if not settings.drm_token and refresher is not None:
            try:
                refreshed = asyncio.run(_run_drm_refresher(refresher))
            except ValueError as exc:
                raise RuntimeError("DRM token refresh returned no token.") from exc
            except Exception as exc:
                raise RuntimeError("DRM token refresh failed before license request.") from exc
            if refreshed is None or not refreshed.token:
                raise RuntimeError("DRM token refresh returned no token.")
            source.drm = refreshed

        # Normalize license input only after the late refresh so the challenge uses
        # the newest provider token. Explicit CLI values still win in the normalizer.
        try:
            license_input = normalize_widevine_license_input(
                source.drm,  # type: ignore[arg-type]
                override_license_url=settings.drm_license_server,
                override_token=settings.drm_token,
                extra_headers=extra_headers,
            )
        except Exception as exc:
            raise RuntimeError(f"DRM license input error: {exc}") from exc

        # Decrypt via the proof pipeline.
        final_output = dest.parent / f"{dest.name}.mp4"
        try:
            result = asyncio.run(
                prove_decrypt_path(
                    license_input=license_input,
                    device_path=device_path,
                    encrypted_path=encrypted_paths,
                    output_path=final_output,
                    license_post=post_license_challenge,
                    validate_output=True,
                    overwrite=settings.overwrite,
                )
            )
        except Exception as exc:
            # Keep encrypted artifact on failure.
            raise RuntimeError(
                "DRM decryption failed. Encrypted artifact was kept for manual diagnosis."
            ) from exc

        # Remove encrypted staging on success.
        for encrypted_path in encrypted_paths:
            with contextlib.suppress(OSError):
                encrypted_path.unlink()

        return result.output_path

    @staticmethod
    def _encrypted_artifacts(dest: Path, staging_id: str) -> list[Path]:
        """Return compatible encrypted tracks in deterministic path order."""
        prefix = f"{dest.name}.encrypted.{staging_id}."
        return sorted(
            [
                path
                for path in dest.parent.iterdir()
                if (
                    path.is_file()
                    and path.name.startswith(prefix)
                    and path.suffix.lower() in _DRM_ARTIFACT_EXTENSIONS
                )
            ],
            key=lambda path: path.name,
        )

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
