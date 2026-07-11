"""Extractor de Udemy — enumera vía la API 2.0 y descarga con yt-dlp.

Udemy está detrás de Cloudflare Turnstile, que detecta el CDP de cualquier
navegador automatizado (Playwright) y entra en un loop de verificación. Por eso
NO se navega Udemy con un navegador: se usan las cookies del navegador REAL del
usuario (``--cookies-from-browser``), donde ya pasó Cloudflare como humano.

Por qué NO se delega el listado en ``udemy:course`` de yt-dlp: ese extractor
saca el ``course_id`` de la página con regex (``data-course-id``...). Udemy está
migrando las páginas de curso a React Server Components y esos patrones ya no
matchean, así que ``udemy:course`` falla con ``Unable to extract course id``.

Flujo (independiente del HTML del curso):
* ``list_course``: resuelve el ``course_id`` (query o página, cubriendo markup
  viejo y nuevo) y enumera el currículum con la API 2.0
  (``cached-subscriber-curriculum-items``) — el mismo endpoint que yt-dlp usa
  internamente. Cada lección se emite como una URL "smuggleada" con el
  ``course_id``, idéntica a la que produce yt-dlp.
* ``resolve_video``: no navega; devuelve un ``VideoSource`` que apunta a esa URL
  para que el downloader (yt-dlp) la resuelva. Como el ``course_id`` viaja
  smuggleado, yt-dlp NO vuelve a scrapear el HTML: lo lee del fragmento.

DRM: yt-dlp reporta las lecciones protegidas como sin formatos descargables; el
núcleo lo registra como fallo de esa clase y continúa con el resto.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlencode, urlsplit

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
# Patrones para hallar el course_id en la página del curso. Se cubren el markup
# clásico (data-course-id / courseId) y el nuevo (RSC), donde el id sólo aparece
# en el deeplink "udemy://discover?courseId=6905411".
_COURSE_ID_PAGE_RES = (
    re.compile(r'data-course-id=["\'](\d+)'),
    re.compile(r"&quot;courseId&quot;\s*:\s*(\d+)"),
    re.compile(r'"courseId"\s*:\s*(\d+)'),
    re.compile(r"courseId=(\d+)"),
)


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

    # -- Estructura del curso ------------------------------------------------
    async def list_course(self, ctx: BrowserContext | None, url: str) -> Course:
        if not self._cookies_from_browser:
            raise ValueError(
                "Udemy requiere --cookies-from-browser <navegador> "
                "(chrome, brave, safari...) para autenticar la sesión."
            )
        course_id = await self._resolve_course_id(url)
        if not course_id:
            raise ValueError(
                "No se pudo determinar el course_id de Udemy. Verifica la URL "
                "del curso y que la sesión del navegador esté activa."
            )
        items = await self._fetch_curriculum(course_id)
        title = await self._fetch_course_title(course_id)
        return self._build_course(url, course_id, items, title_override=title)

    async def _resolve_course_id(self, url: str) -> str | None:
        """Obtiene el course_id del query de la URL o de la página del curso.

        La página se parsea con patrones que cubren el markup clásico y el nuevo
        (RSC), de modo que no dependemos del regex roto de yt-dlp.
        """
        qs = parse_qs(urlsplit(url).query).get("course_id")
        if qs:
            return qs[0]
        html = await self._fetch_text(url)
        for pattern in _COURSE_ID_PAGE_RES:
            m = pattern.search(html)
            if m:
                return m.group(1)
        return None

    async def _fetch_curriculum(self, course_id: str) -> list[dict[str, Any]]:
        """Enumera capítulos y lecciones con la API 2.0 (paginando si hace falta)."""
        params = urlencode(
            {
                "page_size": "1000",
                "fields[chapter]": "title,object_index",
                "fields[lecture]": "title,asset",
                "fields[asset]": "asset_type",
            }
        )
        url: str | None = (
            f"https://www.udemy.com/api-2.0/courses/{course_id}"
            f"/cached-subscriber-curriculum-items/?{params}"
        )
        headers = self._api_headers()
        results: list[dict[str, Any]] = []
        while url:
            try:
                resp = await self._rnet_client().get(url, headers=headers)
                data = json.loads(await resp.text())
            except Exception:  # noqa: BLE001
                break
            results.extend(data.get("results") or [])
            url = data.get("next")
        return results

    def _build_course(
        self,
        url: str,
        course_id: str,
        items: list[dict[str, Any]],
        *,
        title_override: str | None = None,
    ) -> Course:
        """Agrupa el currículum de la API 2.0 en capítulos.

        Cada lección de video se emite como una URL "smuggleada" con el
        ``course_id`` (formato idéntico al de yt-dlp), para que el downloader la
        resuelva sin scrapear el HTML del curso.
        """
        from yt_dlp.utils import smuggle_url

        title = (title_override or "Curso").strip()
        # Primer segmento de la ruta ("course"): réplica de UdemyIE._match_id.
        course_path = urlsplit(url).path.strip("/").split("/")[0] or "course"
        chapters: list[Chapter] = []
        current: Chapter | None = None
        unit_index = 0

        for entry in items:
            clazz = entry.get("_class")
            if clazz == "chapter":
                current = Chapter(
                    title=(entry.get("title") or f"Sección {len(chapters) + 1}").strip(),
                    index=len(chapters) + 1,
                    units=[],
                )
                chapters.append(current)
                continue
            if clazz != "lecture":
                continue

            asset = entry.get("asset") or {}
            if (asset.get("asset_type") or asset.get("assetType")) != "Video":
                continue
            lecture_id = entry.get("id")
            if not lecture_id:
                continue

            if current is None:  # lecciones sueltas antes de cualquier capítulo
                current = Chapter(title="Sección 1", index=1, units=[])
                chapters.append(current)

            unit_index += 1
            lecture_url = smuggle_url(
                f"https://www.udemy.com/{course_path}/learn/v4/t/lecture/{lecture_id}",
                {"course_id": str(course_id)},
            )
            current.units.append(
                Unit(
                    title=(entry.get("title") or f"Clase {unit_index}").strip(),
                    url=lecture_url,
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

    async def _fetch_text(self, url: str) -> str:
        """Descarga el HTML de una página de udemy.com con las cookies del navegador.

        ``allow_redirects``: la URL del curso sin ``/`` final responde 301; sin
        seguir el redirect, rnet devolvería un cuerpo vacío.
        """
        headers = {
            "Cookie": self._udemy_cookie_header(),
            "Referer": "https://www.udemy.com/",
        }
        try:
            resp = await self._rnet_client().get(url, headers=headers, allow_redirects=True)
            return await resp.text()
        except Exception:  # noqa: BLE001
            return ""

    def _api_headers(self) -> dict[str, str]:
        """Headers para las llamadas a la API 2.0 (autenticadas por cookies)."""
        return {
            "Cookie": self._udemy_cookie_header(),
            "Referer": "https://www.udemy.com/",
            "X-Requested-With": "XMLHttpRequest",
        }

    async def _fetch_course_title(self, course_id: str) -> str | None:
        url = f"https://www.udemy.com/api-2.0/courses/{course_id}/?fields[course]=title"
        try:
            resp = await self._rnet_client().get(url, headers=self._api_headers())
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
        try:
            resp = await self._rnet_client().get(url, headers=self._api_headers())
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
