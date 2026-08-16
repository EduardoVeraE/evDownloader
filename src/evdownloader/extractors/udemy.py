"""Extractor de Udemy — enumera vía la API 2.0 y descarga con yt-dlp.

Udemy está detrás de Cloudflare Turnstile, que detecta el CDP de cualquier
navegador automatizado (Playwright) y entra en un loop de verificación. Por eso
NO se navega Udemy con un navegador: se prioriza la sesión guardada por
``evd login udemy`` y se usa ``--cookies-from-browser`` como fallback explícito.

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

import asyncio
import contextlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlencode, urljoin, urlsplit

import rnet
from playwright.async_api import BrowserContext

from .. import browser
from ..config import RNET_IMPERSONATE, UDEMY_BASE_URL, UDEMY_LOGIN_URL, Settings
from ..drm import UDEMY_WIDEVINE_PROXY_URL, detect_drm
from ..drm.token_cache import DrmTokenCache
from ..models import (
    Chapter,
    Course,
    DrmInfo,
    DrmRefresher,
    Resource,
    ResourceKind,
    Unit,
    UnitExtras,
    UnitType,
    VideoSource,
)
from ..url_policy import URLPolicy
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
_DRM_REFRESH_ATTEMPTS = 3
_DRM_REFRESH_BACKOFF_SECONDS = (1.0, 2.0)
_MAX_REDIRECTS = 5
# The API requests 1,000 items per page; this permits up to 100,000 curriculum
# entries while bounding unique pagination URLs controlled by the provider.
_MAX_CURRICULUM_PAGES = 100
_UDEMY_PROVIDER_HOSTS = ("udemy.com",)
_UDEMY_MEDIA_HOSTS = (*_UDEMY_PROVIDER_HOSTS, "udemycdn.com")
_UDEMY_MEDIA_POLICY = URLPolicy("udemy", _UDEMY_MEDIA_HOSTS)


class UdemyExtractor(Extractor):
    # Supplementary assets currently use Udemy's owned CDN. New delivery domains
    # intentionally fail closed until reviewed instead of allowing generic CDNs.
    resource_host_suffixes = ("udemycdn.com",)
    name = "udemy"
    url_policy = URLPolicy(name, _UDEMY_PROVIDER_HOSTS)
    structure_cache_revision = "articles-v1"
    # No usa navegador: delega en yt-dlp (evita el Cloudflare Turnstile).
    needs_browser = False
    login_url = UDEMY_LOGIN_URL
    home_url = UDEMY_BASE_URL
    auth_ready_selector = '[data-purpose="user-avatar"]'

    def __init__(self) -> None:
        self._cookies_from_browser: str | None = None
        self._use_drm = False
        self._drm_license_server: str | None = None
        self._drm_token: str | None = None
        self._cookies: list[dict[str, Any]] = []
        self._cookies_loaded = False
        self._client: rnet.Client | None = None
        self._token_cache = DrmTokenCache()

    def configure(self, settings: Settings) -> None:
        self._cookies_from_browser = settings.cookies_from_browser
        self._use_drm = settings.use_drm
        self._drm_license_server = settings.drm_license_server
        self._drm_token = settings.drm_token
        self._cookies = []
        self._cookies_loaded = False

    # -- Estructura del curso ------------------------------------------------
    async def list_course(self, ctx: BrowserContext | None, url: str) -> Course:
        if not self.url_policy.allows(url):
            raise ValueError("Udemy rechazó una URL de curso no confiable.")
        if not self._load_cookies(required=True):
            raise ValueError(
                "Udemy no tiene una sesión disponible. Ejecuta `evd login udemy` "
                "o usa --cookies-from-browser <navegador> (chrome, brave, safari...)."
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

    async def verify_session(self, cookies: Sequence[Mapping[str, Any]]) -> bool | None:
        """Confirma con la API 2.0 que las cookies son de una sesión real.

        Udemy emite un ``access_token`` de invitado apenas se carga la página,
        antes del login. Verificar contra ``/users/me/`` evita aceptar (y
        persistir) esa sesión anónima: solo devuelve ``True`` si la API
        reconoce a un usuario autenticado.
        """
        url = f"{UDEMY_BASE_URL}/api-2.0/users/me/?fields[user]=id"
        header = browser.cookie_header_for_url(
            browser.cookies_as_records(browser.filter_cookies(self.name, cookies)),
            url,
            trusted_host_suffixes=self.url_policy.host_suffixes,
        )
        if not header:
            return False
        headers = {
            "Cookie": header,
            "Referer": "https://www.udemy.com/",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            resp = await self._rnet_client().get(
                url,
                headers=headers,
                allow_redirects=False,
            )
        except Exception:  # noqa: BLE001
            return False
        try:
            data = json.loads(await resp.text())
        except Exception:  # noqa: BLE001
            return False
        finally:
            with contextlib.suppress(Exception):
                await resp.close()
        return isinstance(data, dict) and data.get("_class") == "user" and bool(data.get("id"))

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
        results: list[dict[str, Any]] = []
        visited: set[str] = set()
        pages = 0
        while url:
            if pages >= _MAX_CURRICULUM_PAGES:
                raise ValueError("Udemy excedió el límite seguro de paginación del currículum.")
            if not self.url_policy.allows(url):
                raise ValueError("Udemy devolvió una paginación fuera del proveedor.")
            if url in visited:
                raise ValueError("Udemy devolvió una paginación de currículum inválida.")
            visited.add(url)
            pages += 1
            try:
                resp = await self._rnet_client().get(
                    url, headers=self._api_headers(url), allow_redirects=False
                )
                data = json.loads(await self._response_text(resp))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    "No se pudo obtener el currículum de Udemy. Inténtalo de nuevo."
                ) from exc
            if not isinstance(data, dict):
                raise ValueError("Udemy devolvió un currículum con formato inválido.")
            page = data.get("results")
            next_url = data.get("next")
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise ValueError("Udemy devolvió un currículum con formato inválido.")
            if next_url is not None and not isinstance(next_url, str):
                raise ValueError("Udemy devolvió una paginación de currículum inválida.")
            results.extend(page)
            url = urljoin(url, next_url) if next_url else None
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

        Cada video o artículo se emite como una URL "smuggleada" con el
        ``course_id`` (formato idéntico al de yt-dlp). Los artículos conservan su
        lugar en el currículum y resuelven su cuerpo solo cuando se piden extras.
        """
        from yt_dlp.utils import smuggle_url

        title = (title_override or "Curso").strip()
        # Primer segmento de la ruta ("course"): réplica de UdemyIE._match_id.
        course_path = urlsplit(url).path.strip("/").split("/")[0] or "course"
        chapters: list[Chapter] = []
        current: Chapter | None = None
        lecture_index = 0

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

            lecture_index += 1
            asset = entry.get("asset") or {}
            unit_type = self._classify_asset(asset)
            if unit_type is None:
                continue
            lecture_id = entry.get("id")
            if not lecture_id:
                continue

            if current is None:  # lecciones sueltas antes de cualquier capítulo
                current = Chapter(title="Sección 1", index=1, units=[])
                chapters.append(current)

            lecture_url = smuggle_url(
                f"https://www.udemy.com/{course_path}/learn/v4/t/lecture/{lecture_id}",
                {"course_id": str(course_id)},
            )
            current.units.append(
                Unit(
                    title=(entry.get("title") or f"Clase {lecture_index}").strip(),
                    url=lecture_url,
                    type=unit_type,
                    index=lecture_index,
                )
            )

        return Course(title=title, url=url, chapters=chapters)

    @staticmethod
    def _classify_asset(asset: Mapping[str, Any]) -> UnitType | None:
        asset_type = asset.get("asset_type") or asset.get("assetType")
        if asset_type == "Video":
            return UnitType.VIDEO
        if asset_type == "Article":
            return UnitType.LECTURE
        return None

    # -- Resolución de la fuente de video -----------------------------------
    async def resolve_video(self, ctx: BrowserContext | None, unit: Unit) -> VideoSource | None:
        if unit.type != UnitType.VIDEO or not unit.url:
            return None
        if not self.url_policy.allows(unit.url):
            raise ValueError("Udemy rechazó una URL de clase no confiable.")
        cookies = self._load_cookies(required=True)
        if not cookies:
            raise ValueError(
                "Udemy no tiene una sesión utilizable. Ejecuta `evd login udemy` "
                "o proporciona un navegador con una sesión activa."
            )
        # Por defecto no se resuelve aquí: el downloader (yt-dlp) toma la URL de
        # la lección y la resuelve con las cookies de la misma fuente. write_subs=True:
        # yt-dlp baja también los subtítulos de la lección.
        source = VideoSource(
            url=unit.url,
            is_embed=True,
            cookies=browser.cookies_as_dict(cookies),
            cookie_jar=browser.cookies_as_records(cookies),
            write_subs=True,
        )
        if self._use_drm:
            await self._attach_drm(unit, source)
        return source

    async def _attach_drm(self, unit: Unit, source: VideoSource) -> None:
        """Populate VideoSource.drm for DRM-protected Udemy lectures."""
        if not unit.url:
            return
        if not self.url_policy.allows(unit.url):
            raise ValueError("Udemy rechazó una URL de clase no confiable.")
        cookies = self._load_cookies()
        if not cookies and not self._cookies_from_browser:
            return
        course_id, lecture_id = self._ids_from_url(unit.url)
        if not course_id or not lecture_id:
            return

        asset = self._token_cache.get(course_id, lecture_id)
        if asset is None:
            asset = await self._fetch_drm_asset(course_id, lecture_id)
            self._token_cache.put(course_id, lecture_id, asset)
        if not asset.get("course_is_drmed"):
            return

        mpd_url = self._dash_manifest_url(asset.get("media_sources") or [])
        if not mpd_url:
            return

        manifest = await self._fetch_text(mpd_url)
        detected = detect_drm(manifest, url=mpd_url).systems
        drm = next((info for info in detected if info.scheme == "widevine"), None)
        if drm is None:
            drm = detected[0] if detected else None
        if drm is None:
            return

        # Apply provider token (asset-level) first.
        token = asset.get("media_license_token")
        if isinstance(token, str) and token:
            drm.token = token

        # CLI license server override > detected > Udemy proxy default.
        if self._drm_license_server:
            drm.license_url = self._drm_license_server
        elif not drm.license_url:
            drm.license_url = UDEMY_WIDEVINE_PROXY_URL

        # CLI token override > provider token (already in drm.token).
        if self._drm_token:
            drm.token = self._drm_token

        source.drm = drm
        source.drm_refresher = self._build_drm_refresher(course_id, lecture_id, drm)

        # DRM mode: yt-dlp receives the MPD directly, not the lecture page.
        source.url = mpd_url
        source.is_embed = False
        source.write_subs = False

    def _build_drm_refresher(
        self, course_id: str, lecture_id: str, current: DrmInfo
    ) -> DrmRefresher:
        """Build a late asset refresh callback without performing another request now."""
        source_loop = asyncio.get_running_loop()

        async def refresh_on_source_loop() -> DrmInfo:
            for attempt in range(_DRM_REFRESH_ATTEMPTS):
                try:
                    asset = await self._fetch_drm_asset(course_id, lecture_id)
                    token = asset.get("media_license_token")
                    if isinstance(token, str) and token:
                        return current.model_copy(update={"token": token})
                except Exception:  # noqa: BLE001
                    pass

                if attempt < _DRM_REFRESH_ATTEMPTS - 1:
                    await asyncio.sleep(_DRM_REFRESH_BACKOFF_SECONDS[attempt])

            raise ValueError("Udemy did not return a fresh DRM media license token.")

        async def refresh() -> DrmInfo:
            if source_loop.is_closed() or not source_loop.is_running():
                raise RuntimeError("Udemy DRM refresh loop is no longer running.")
            if asyncio.get_running_loop() is source_loop:
                return await refresh_on_source_loop()
            future = asyncio.run_coroutine_threadsafe(refresh_on_source_loop(), source_loop)
            return await asyncio.wrap_future(future)

        return refresh

    async def _fetch_drm_asset(self, course_id: str, lecture_id: str) -> dict[str, Any]:
        """Fetch only the Udemy asset fields needed for DRM detection."""
        url = (
            f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}"
            f"/lectures/{lecture_id}/?fields[lecture]=asset"
            f"&fields[asset]=asset_type,course_is_drmed,media_license_token,media_sources"
        )
        try:
            if not self.url_policy.allows(url):
                return {}
            resp = await self._rnet_client().get(
                url, headers=self._api_headers(url), allow_redirects=False
            )
            data = json.loads(await self._response_text(resp))
        except Exception:  # noqa: BLE001
            return {}
        asset = data.get("asset")
        return asset if isinstance(asset, dict) else {}

    @staticmethod
    def _dash_manifest_url(media_sources: list[dict[str, Any]]) -> str | None:
        """Return the DASH MPD URL from Udemy media_sources, if present."""
        for source in media_sources:
            if source.get("type") == "application/dash+xml" and source.get("src"):
                url = str(source["src"])
                return url if _UDEMY_MEDIA_POLICY.allows(url) else None
        return None

    # -- Material complementario (recursos adjuntos y enlaces) ---------------
    async def resolve_extras(
        self, ctx: BrowserContext | None, unit: Unit, *, capture_page: bool = False
    ) -> UnitExtras:
        """Cuerpo de artículos y recursos suplementarios de la lección.

        Se consultan en la API 2.0 de Udemy con las cookies del navegador. Las
        URLs de descarga que devuelve Udemy están firmadas (no requieren cookies
        para bajarlas). No se captura MHTML (no hay navegador).
        """
        if not unit.url or not self._load_cookies():
            return UnitExtras()
        if not self.url_policy.allows(unit.url):
            raise ValueError("Udemy rechazó una URL de clase no confiable.")
        course_id, lecture_id = self._ids_from_url(unit.url)
        if not course_id or not lecture_id:
            return UnitExtras()
        details = await self._fetch_lecture_details(
            course_id, lecture_id, include_article=unit.type is UnitType.LECTURE
        )
        assets = details.get("supplementary_assets")
        if not isinstance(assets, list):
            assets = []

        summary_html: str | None = None
        primary_asset = details.get("asset")
        if (
            unit.type is UnitType.LECTURE
            and isinstance(primary_asset, dict)
            and self._classify_asset(primary_asset) is UnitType.LECTURE
        ):
            body = primary_asset.get("body")
            if isinstance(body, str) and body:
                summary_html = body

        return UnitExtras(
            summary_html=summary_html,
            resources=self._assets_to_resources(
                [asset for asset in assets if isinstance(asset, dict)]
            ),
        )

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
        """Fetch provider/CDN text through validated, credential-scoped redirects."""
        current_url = url
        visited: set[str] = set()
        for hop in range(_MAX_REDIRECTS + 1):
            if not _UDEMY_MEDIA_POLICY.allows(current_url) or current_url in visited:
                return ""
            visited.add(current_url)
            headers = {"Referer": "https://www.udemy.com/"}
            cookie = self._udemy_cookie_header(current_url)
            if cookie:
                headers["Cookie"] = cookie
            try:
                resp = await self._rnet_client().get(
                    current_url, headers=headers, allow_redirects=False
                )
            except Exception:  # noqa: BLE001
                return ""
            try:
                status = resp.status_code.as_int()
                if status in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location")
                    if not isinstance(location, str) or not location or hop == _MAX_REDIRECTS:
                        return ""
                    current_url = urljoin(current_url, location)
                    continue
                if not 200 <= status < 300:
                    return ""
                return await resp.text()
            except Exception:  # noqa: BLE001
                return ""
            finally:
                with contextlib.suppress(Exception):
                    await resp.close()
        return ""

    def _api_headers(self, url: str) -> dict[str, str]:
        """Headers para las llamadas a la API 2.0 (autenticadas por cookies)."""
        headers = {
            "Referer": "https://www.udemy.com/",
            "X-Requested-With": "XMLHttpRequest",
        }
        cookie = self._udemy_cookie_header(url)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _fetch_course_title(self, course_id: str) -> str | None:
        url = f"https://www.udemy.com/api-2.0/courses/{course_id}/?fields[course]=title"
        try:
            if not self.url_policy.allows(url):
                return None
            resp = await self._rnet_client().get(
                url, headers=self._api_headers(url), allow_redirects=False
            )
            data = json.loads(await self._response_text(resp))
        except Exception:  # noqa: BLE001
            return None
        return (data.get("title") or "").strip() or None

    async def _fetch_lecture_details(
        self, course_id: str, lecture_id: str, *, include_article: bool
    ) -> dict[str, Any]:
        lecture_fields = "asset,supplementary_assets" if include_article else "supplementary_assets"
        asset_fields = "asset_type,title,filename,download_urls,external_url"
        if include_article:
            asset_fields += ",body"
        params = urlencode(
            {
                "fields[lecture]": lecture_fields,
                "fields[asset]": asset_fields,
            }
        )
        url = (
            f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}"
            f"/lectures/{lecture_id}/?{params}"
        )
        try:
            if not self.url_policy.allows(url):
                return {}
            resp = await self._rnet_client().get(
                url, headers=self._api_headers(url), allow_redirects=False
            )
            data = json.loads(await self._response_text(resp))
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    async def _response_text(response: Any) -> str:
        try:
            return await response.text()
        finally:
            with contextlib.suppress(Exception):
                await response.close()

    def _rnet_client(self) -> rnet.Client:
        if self._client is None:
            self._client = rnet.Client(
                impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None)
            )
        return self._client

    def _udemy_cookie_header(self, url: str) -> str:
        """Build an RFC-scoped Cookie header only inside Udemy's authority."""
        return (
            browser.cookie_header_for_url(
                browser.cookies_as_records(self._load_cookies()),
                url,
                trusted_host_suffixes=self.url_policy.host_suffixes,
            )
            or ""
        )

    def _load_cookies(self, *, required: bool = False) -> list[dict[str, Any]]:
        """Prioriza cookies frescas del navegador configurado.

        Usa la sesión persistida como respaldo si el navegador no aporta una sesión utilizable.
        """
        if not self._cookies_loaded:
            try:
                self._cookies = browser.filter_cookies(
                    "udemy",
                    browser.resolve_cookies("udemy", self._cookies_from_browser),
                )
            except ValueError:
                if required:
                    raise
                self._cookies = []
            self._cookies_loaded = True
        if required and not self._cookies:
            return []
        return self._cookies
