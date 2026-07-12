"""Extractor de Codigofacilito — temario SSR + video delegado a yt-dlp.

Codigofacilito (app Rails renderizada en servidor) es un caso híbrido:

* **Estructura del curso**: el temario viaja en el HTML de ``/cursos/{slug}``
  (server-side rendered), así que se descarga con ``rnet`` y se parsea con
  expresiones regulares acotadas — sin abrir navegador. Cada módulo es un
  ``<header>`` con ``Módulo N`` + ``<h4>`` de título, seguido de una lista de
  ``<a href="/videos/{slug}">`` cuyo título está en ``.topic-item-title``.
* **Fuente de video**: cada clase ``/videos/{slug}`` embebe un reproductor de
  **BunnyCDN** (``player.mediadelivery.net/embed/...``). yt-dlp NO tiene un
  extractor con el nombre de la plataforma, pero su extractor *genérico* detecta
  el embed y delega en el extractor nativo ``BunnyCdn`` (que resuelve el m3u8 y
  el Referer). Por eso ``resolve_video`` no navega: devuelve la URL de la clase
  y deja que el downloader (yt-dlp) la resuelva con las cookies del navegador
  real del usuario (``--cookies-from-browser``), donde ya inició sesión.

Al no automatizar el navegador se evita el modo challenge de Cloudflare y se
reutiliza toda la infraestructura de yt-dlp del proyecto (igual que Udemy).
"""

from __future__ import annotations

import html as html_lib
import re

import rnet
from playwright.async_api import BrowserContext

from ..config import (
    CODIGOFACILITO_BASE_URL,
    CODIGOFACILITO_LOGIN_URL,
    DEFAULT_USER_AGENT,
    RNET_IMPERSONATE,
    Settings,
)
from ..models import Chapter, Course, Unit, UnitType, VideoSource
from .base import Extractor

# Título del curso: el primer <h1> de la página del curso.
_TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)

# Cabecera de módulo: ``... Módulo N </span> ... <h4 ...>Título del módulo</h4>``.
_MODULE_RE = re.compile(r"Módulo\s*(\d+)\s*</span>.*?<h4[^>]*>(.*?)</h4>", re.S)

# Clase del temario: enlace ``/videos/{slug}`` con su título (``topic-item-title``).
# El ``<li>`` autenticado añade modificadores de progreso a la clase
# (``topic-item--completed``, ``topic-item--active``), por eso se tolera
# ``topic-item[^']*``. El icono NO se usa para clasificar: en la vista con sesión
# refleja el progreso (``done_all`` en clases vistas), no el tipo de contenido.
_CLASS_RE = re.compile(
    r'<a href="(/videos/[^"]+)"[^>]*>\s*<li class=\'topic-item[^\']*\'.*?'
    r"topic-item-title'>(.*?)</p>",
    re.S,
)

# Token combinado: recorre módulos y clases EN ORDEN de aparición en el
# documento para agrupar cada clase bajo su módulo (el temario es secuencial).
_TOKEN_RE = re.compile(_MODULE_RE.pattern + "|" + _CLASS_RE.pattern, re.S)


class CodigofacilitoExtractor(Extractor):
    name = "codigofacilito"
    # No usa navegador: el temario es SSR (rnet) y el video lo resuelve yt-dlp.
    needs_browser = False
    login_url = CODIGOFACILITO_LOGIN_URL
    home_url = CODIGOFACILITO_BASE_URL
    auth_ready_selector = (
        'a[href*="/users/sign_out"], '
        'form[action*="/users/sign_out"], '
        'a[href*="/dashboard"], '
        'a[href*="/perfil"]'
    )

    def __init__(self) -> None:
        self._cookies_from_browser: str | None = None
        self._cookie_header: str | None = None
        self._client: rnet.Client | None = None

    def configure(self, settings: Settings) -> None:
        self._cookies_from_browser = settings.cookies_from_browser

    @staticmethod
    def supports(url: str) -> bool:
        return "codigofacilito.com" in url

    # -- Estructura del curso ------------------------------------------------
    async def list_course(self, ctx: BrowserContext | None, url: str) -> Course:
        if not self._cookies_from_browser:
            raise ValueError(
                "Codigofacilito requiere --cookies-from-browser <navegador> "
                "(chrome, brave, safari...): las clases están tras el login de pago."
            )
        html = await self._fetch(url)
        return self._parse_course(url, html)

    async def _fetch(self, url: str) -> str:
        """Descarga el HTML del curso con las cookies del navegador del usuario."""
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": CODIGOFACILITO_BASE_URL + "/",
        }
        cookie = self._cf_cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        resp = await self._rnet_client().get(url, headers=headers)
        return await resp.text()

    @classmethod
    def _parse_course(cls, url: str, html: str) -> Course:
        """Construye el ``Course`` a partir del HTML SSR del temario.

        Función pura (testeable con un fixture): recorre el token combinado de
        módulos y clases en orden, abriendo un capítulo por cada ``Módulo N`` y
        colgando de él las clases que le siguen hasta el próximo módulo.
        """
        title_m = _TITLE_RE.search(html)
        title = cls._clean(title_m.group(1)) if title_m else "Curso"

        chapters: list[Chapter] = []
        seen: set[str] = set()
        current: Chapter | None = None
        unit_index = 0

        for m in _TOKEN_RE.finditer(html):
            if m.group(1) is not None:  # cabecera de módulo
                current = Chapter(
                    title=cls._clean(m.group(2)) or f"Módulo {len(chapters) + 1}",
                    index=len(chapters) + 1,
                    units=[],
                )
                chapters.append(current)
                continue

            href, raw_title = m.group(3), m.group(4)
            full_url = f"{CODIGOFACILITO_BASE_URL}{href}"
            if full_url in seen:
                continue
            seen.add(full_url)

            # Clases fuera de cualquier módulo (raro): agruparlas en uno genérico.
            if current is None:
                current = Chapter(title="Contenido", index=1, units=[])
                chapters.append(current)

            unit_index += 1
            # Toda entrada del temario es una lección en video (``/videos/{slug}``);
            # si alguna no tuviera video, yt-dlp lo reporta y el núcleo lo omite.
            current.units.append(
                Unit(
                    title=cls._clean(raw_title) or f"Clase {unit_index}",
                    url=full_url,
                    type=UnitType.VIDEO,
                    index=unit_index,
                )
            )

        # Descartar módulos que quedaron sin clases (p. ej. secciones vacías).
        chapters = [c for c in chapters if c.units]
        return Course(title=title, url=url, chapters=chapters)

    @staticmethod
    def _clean(raw: str) -> str:
        """Quita etiquetas HTML, desescapa entidades y normaliza espacios."""
        text = re.sub(r"<[^>]+>", "", raw)
        return html_lib.unescape(text).strip()

    # -- Resolución de la fuente de video -----------------------------------
    async def resolve_video(
        self, ctx: BrowserContext | None, unit: Unit
    ) -> VideoSource | None:
        if unit.type != UnitType.VIDEO or not unit.url:
            return None
        # No se resuelve aquí: el downloader (yt-dlp) toma la URL de la clase,
        # detecta el embed de BunnyCDN y lo descarga con las cookies del
        # navegador (settings.cookies_from_browser).
        return VideoSource(url=unit.url, is_embed=True, write_subs=True)

    # -- Auxiliares de red ---------------------------------------------------
    def _rnet_client(self) -> rnet.Client:
        if self._client is None:
            self._client = rnet.Client(
                impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None)
            )
        return self._client

    def _cf_cookie_header(self) -> str:
        """Construye (una vez) el header Cookie de codigofacilito.com desde el navegador."""
        if self._cookie_header is None:
            if not self._cookies_from_browser:
                self._cookie_header = ""
            else:
                from yt_dlp.cookies import extract_cookies_from_browser

                jar = extract_cookies_from_browser(self._cookies_from_browser)
                self._cookie_header = "; ".join(
                    f"{c.name}={c.value}"
                    for c in jar
                    if "codigofacilito.com" in (c.domain or "")
                )
        return self._cookie_header
