"""Extractor de Udemy — delega en los extractores nativos de yt-dlp.

Udemy está detrás de Cloudflare Turnstile, que detecta el CDP de cualquier
navegador automatizado (Playwright) y entra en un loop de verificación. Por eso
NO se navega Udemy con un navegador: se delega en los extractores nativos de
yt-dlp (``udemy`` / ``udemy:course``), que hablan con la API de Udemy usando las
cookies del navegador REAL del usuario (``--cookies-from-browser``), donde ya
pasó Cloudflare como humano.

Flujo:
* ``list_course``: yt-dlp en modo *flat* devuelve la lista de lecciones con su
  capítulo; se agrupan en ``Course``.
* ``resolve_video``: no navega; devuelve un ``VideoSource`` que apunta a la URL
  de la lección para que el downloader (yt-dlp) la resuelva y descargue con las
  cookies del navegador (``settings.cookies_from_browser``).

DRM: yt-dlp reporta las lecciones protegidas como sin formatos descargables; el
núcleo lo registra como fallo de esa clase y continúa con el resto.
"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import BrowserContext

from ..config import UDEMY_BASE_URL, UDEMY_LOGIN_URL, Settings
from ..models import Chapter, Course, Unit, UnitType, VideoSource
from .base import Extractor


class UdemyExtractor(Extractor):
    name = "udemy"
    # No usa navegador: delega en yt-dlp (evita el Cloudflare Turnstile).
    needs_browser = False
    login_url = UDEMY_LOGIN_URL
    home_url = UDEMY_BASE_URL

    def __init__(self) -> None:
        self._cookies_from_browser: str | None = None

    def configure(self, settings: Settings) -> None:
        self._cookies_from_browser = settings.cookies_from_browser

    @staticmethod
    def supports(url: str) -> bool:
        return "udemy.com" in url

    def _ydl_opts(self, **extra: Any) -> dict[str, Any]:
        opts: dict[str, Any] = {"quiet": True, "no_warnings": True, **extra}
        if self._cookies_from_browser:
            opts["cookiesfrombrowser"] = (self._cookies_from_browser, None, None, None)
        return opts

    # -- Estructura del curso ------------------------------------------------
    async def list_course(self, ctx: BrowserContext | None, url: str) -> Course:
        if not self._cookies_from_browser:
            raise ValueError(
                "Udemy requiere --cookies-from-browser <navegador> "
                "(chrome, brave, safari...) para autenticar la sesión."
            )
        info = await asyncio.to_thread(self._extract_flat, url)
        return self._build_course(url, info)

    def _extract_flat(self, url: str) -> dict[str, Any]:
        """Extrae la estructura del curso con yt-dlp en modo flat (sin resolver)."""
        import yt_dlp

        opts = self._ydl_opts(extract_flat="in_playlist", skip_download=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}

    def _build_course(self, url: str, info: dict[str, Any]) -> Course:
        """Agrupa las lecciones (planas) de yt-dlp en capítulos."""
        title = (info.get("title") or "Curso").strip()
        chapters: list[Chapter] = []
        seen: set[str] = set()
        unit_index = 0
        current: Chapter | None = None
        current_key: object = object()  # centinela: fuerza abrir el primer capítulo

        for entry in info.get("entries", []) or []:
            entry_url = entry.get("url") or entry.get("webpage_url") or ""
            if not entry_url or entry_url in seen:
                continue
            seen.add(entry_url)

            ch_key = entry.get("chapter_number") or entry.get("chapter") or 0
            if ch_key != current_key:
                current_key = ch_key
                current = Chapter(
                    title=(entry.get("chapter") or f"Sección {len(chapters) + 1}").strip(),
                    index=len(chapters) + 1,
                    units=[],
                )
                chapters.append(current)

            unit_index += 1
            assert current is not None
            current.units.append(
                Unit(
                    title=(entry.get("title") or f"Clase {unit_index}").strip(),
                    url=entry_url,
                    type=UnitType.VIDEO,
                    index=unit_index,
                )
            )

        return Course(title=title, url=url, chapters=chapters)

    # -- Resolución de la fuente de video -----------------------------------
    async def resolve_video(
        self, ctx: BrowserContext | None, unit: Unit
    ) -> VideoSource | None:
        if unit.type != UnitType.VIDEO or not unit.url:
            return None
        # No se resuelve aquí: el downloader (yt-dlp) toma la URL de la lección y
        # la resuelve con las cookies del navegador (settings.cookies_from_browser).
        return VideoSource(url=unit.url, is_embed=True)
