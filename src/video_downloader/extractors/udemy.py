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
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlsplit

import rnet
from playwright.async_api import BrowserContext

from ..config import RNET_IMPERSONATE, UDEMY_BASE_URL, UDEMY_LOGIN_URL, Settings
from ..models import (
    Chapter,
    Course,
    Resource,
    ResourceKind,
    Unit,
    UnitExtras,
    UnitType,
    VideoSource,
)
from .base import Extractor

_LECTURE_ID_RE = re.compile(r"/lecture/(\d+)")
# course_id viaja "smuggleado" por yt-dlp en el fragmento de la URL de la clase.
_COURSE_ID_RE = re.compile(r'"course_id":\s*"?(\d+)"?')
# Valor con que yt-dlp marca las clases sueltas sin sección nombrada.
_UNNAMED_CHAPTER = "Undefined"


class UdemyExtractor(Extractor):
    name = "udemy"
    # No usa navegador: delega en yt-dlp (evita el Cloudflare Turnstile).
    needs_browser = False
    login_url = UDEMY_LOGIN_URL
    home_url = UDEMY_BASE_URL

    def __init__(self) -> None:
        self._cookies_from_browser: str | None = None
        self._cookie_header: str | None = None
        self._client: rnet.Client | None = None

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
        # yt-dlp suele devolver un título genérico ("Curso"); pedir el real a la API.
        course_id = self._course_id_from(url, info)
        title = await self._fetch_course_title(course_id) if course_id else None
        return self._build_course(url, info, title_override=title)

    def _extract_flat(self, url: str) -> dict[str, Any]:
        """Extrae la estructura del curso con yt-dlp en modo flat (sin resolver)."""
        import yt_dlp

        opts = self._ydl_opts(extract_flat="in_playlist", skip_download=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}

    def _build_course(
        self, url: str, info: dict[str, Any], *, title_override: str | None = None
    ) -> Course:
        """Agrupa las lecciones (planas) de yt-dlp en capítulos."""
        title = (title_override or info.get("title") or "Curso").strip()
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

            ch_name = entry.get("chapter")
            if ch_name == _UNNAMED_CHAPTER:
                ch_name = None
            ch_key = entry.get("chapter_number") or ch_name or 0
            if ch_key != current_key:
                current_key = ch_key
                current = Chapter(
                    title=(ch_name or f"Sección {len(chapters) + 1}").strip(),
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
        # write_subs=True: yt-dlp baja también los subtítulos de la lección.
        return VideoSource(url=unit.url, is_embed=True, write_subs=True)

    # -- Material complementario (recursos adjuntos y enlaces) ---------------
    async def resolve_extras(
        self, ctx: BrowserContext | None, unit: Unit, *, capture_page: bool = False
    ) -> UnitExtras:
        """Recursos suplementarios de la lección (adjuntos y enlaces externos).

        Se consultan en la API 2.0 de Udemy con las cookies del navegador. Las
        URLs de descarga que devuelve Udemy están firmadas (no requieren cookies
        para bajarlas). No se captura MHTML (no hay navegador).
        """
        if not unit.url or not self._cookies_from_browser:
            return UnitExtras()
        course_id, lecture_id = self._ids_from_url(unit.url)
        if not course_id or not lecture_id:
            return UnitExtras()
        assets = await self._fetch_supplementary(course_id, lecture_id)
        return UnitExtras(resources=self._assets_to_resources(assets))

    @staticmethod
    def _ids_from_url(url: str) -> tuple[str | None, str | None]:
        """Extrae ``(course_id, lecture_id)`` de la URL de una lección."""
        lecture = _LECTURE_ID_RE.search(url)
        course = _COURSE_ID_RE.search(unquote_plus(url))
        return (
            course.group(1) if course else None,
            lecture.group(1) if lecture else None,
        )

    @staticmethod
    def _assets_to_resources(assets: list[dict[str, Any]]) -> list[Resource]:
        """Convierte los ``supplementary_assets`` de Udemy en ``Resource``."""
        resources: list[Resource] = []
        for a in assets:
            external = a.get("external_url")
            if external:
                resources.append(
                    Resource(
                        title=(a.get("title") or a.get("filename") or "enlace").strip(),
                        url=external,
                        kind=ResourceKind.LINK,
                    )
                )
                continue
            # download_urls es {asset_type: [{"label", "file"}]}; tomar la 1ª URL.
            file_url = next(
                (
                    v[0]["file"]
                    for v in (a.get("download_urls") or {}).values()
                    if v and v[0].get("file")
                ),
                None,
            )
            if file_url:
                resources.append(
                    Resource(
                        # El filename real evita colisiones (las URLs firmadas
                        # terminan todas en "original.<ext>").
                        title=(a.get("filename") or a.get("title") or "recurso").strip(),
                        url=file_url,
                        kind=ResourceKind.FILE,
                    )
                )
        return resources

    @staticmethod
    def _course_id_from(url: str, info: dict[str, Any]) -> str | None:
        """Obtiene el course_id del query de la URL o del smuggle de una clase."""
        qs = parse_qs(urlsplit(url).query).get("course_id")
        if qs:
            return qs[0]
        for entry in info.get("entries", []) or []:
            course_id, _ = UdemyExtractor._ids_from_url(entry.get("url") or "")
            if course_id:
                return course_id
        return None

    async def _fetch_course_title(self, course_id: str) -> str | None:
        url = f"https://www.udemy.com/api-2.0/courses/{course_id}/?fields[course]=title"
        headers = {
            "Cookie": self._udemy_cookie_header(),
            "Referer": "https://www.udemy.com/",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            resp = await self._rnet_client().get(url, headers=headers)
            data = json.loads(await resp.text())
        except Exception:  # noqa: BLE001
            return None
        return (data.get("title") or "").strip() or None

    async def _fetch_supplementary(self, course_id: str, lecture_id: str) -> list[dict[str, Any]]:
        url = (
            f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}"
            f"/lectures/{lecture_id}/?fields[lecture]=supplementary_assets"
            f"&fields[asset]=asset_type,title,filename,download_urls,external_url"
        )
        headers = {
            "Cookie": self._udemy_cookie_header(),
            "Referer": "https://www.udemy.com/",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            resp = await self._rnet_client().get(url, headers=headers)
            data = json.loads(await resp.text())
        except Exception:  # noqa: BLE001
            return []
        return data.get("supplementary_assets") or []

    def _rnet_client(self) -> rnet.Client:
        if self._client is None:
            self._client = rnet.Client(
                impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None)
            )
        return self._client

    def _udemy_cookie_header(self) -> str:
        """Construye (una vez) el header Cookie de udemy.com desde el navegador."""
        if self._cookie_header is None:
            from yt_dlp.cookies import extract_cookies_from_browser

            assert self._cookies_from_browser is not None
            jar = extract_cookies_from_browser(self._cookies_from_browser)
            self._cookie_header = "; ".join(
                f"{c.name}={c.value}" for c in jar if "udemy.com" in (c.domain or "")
            )
        return self._cookie_header
