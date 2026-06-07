"""Extractor de Platzi.

Estrategia frente a la versión que se rompió:

* **Estructura del curso**: se prioriza el JSON embebido de Next.js
  (``__NEXT_DATA__``) por ser más estable que las clases CSS; si no está
  disponible, se cae a selectores DOM con coincidencia por subcadena.
* **Fuente de video**: en vez de un regex sobre el HTML estático (que ya no
  contiene la URL desde la migración a Mediastream), se **intercepta la red**
  mientras carga el reproductor para capturar el embed de Mediastream
  (``mdstrm.com/embed/{id}``) y/o el master ``.m3u8``, junto con los subtítulos
  ``.vtt`` (descartando los ``.vtt.m3u8``).
"""

from __future__ import annotations

import contextlib
import re

from playwright.async_api import BrowserContext, Request

from .. import browser
from ..config import DEFAULT_USER_AGENT, MEDIASTREAM_HOSTS, PLATZI_BASE_URL
from ..models import (
    Chapter,
    Course,
    Resource,
    ResourceKind,
    Subtitle,
    Unit,
    UnitExtras,
    UnitType,
    VideoSource,
)
from .base import Extractor

# Extrae el temario directamente del DOM renderizado. Cada capítulo es un
# ``ul[class*='Materials']`` cuyo título es el ``h3[class*='Title']`` de su
# sección contenedora; el título limpio de cada clase está en ``img[alt]``.
_SYLLABUS_JS = """() => {
  const courseTitle = (document.querySelector('h1')?.textContent || '').trim();
  const chapters = [];
  document.querySelectorAll("ul[class*='Materials']").forEach(ul => {
    const section = ul.closest("div[class*='SyllabusSection']") || ul.parentElement;
    const h3 = section ? section.querySelector("h3[class*='Title']") : null;
    const chTitle = h3 ? h3.textContent.trim() : '';
    const units = [];
    ul.querySelectorAll("a[class*='ItemLink'], li > a").forEach(a => {
      const href = a.getAttribute('href');
      if (!href) return;
      const img = a.querySelector('img');
      let title = (img && img.alt) ? img.alt.trim() : '';
      if (!title) {
        const c = a.querySelector("[class*='Content'], [class*='Title']");
        title = (c ? c.textContent : a.textContent).trim();
      }
      // Señales para clasificar el tipo SIN navegar a la clase: las clases de
      // video llevan miniatura de Mediastream y un badge de duración.
      const thumb = img ? (img.getAttribute('src') || '') : '';
      const dur = a.querySelector("[class*='ItemDetails__Duration'], [class*='Duration']");
      const duration = dur ? dur.textContent.trim() : '';
      units.push({ href, title, thumb, duration });
    });
    if (units.length) chapters.push({ title: chTitle, units });
  });
  return { title: courseTitle, chapters };
}"""

# Material complementario de una clase: el "Resumen" (lectura) y la sección
# "Recursos" (FilesAndLinks). Clases verificadas en vivo (2026-06-07):
# resumen = ``[class*='Resources_Resources__summary'] [class*='Markdown_Markdown']``;
# recursos = ``[class*='FilesAndLinks'] a[class*='Item__link'][href]`` con título en
# ``[class*='Item__title']``.
_EXTRAS_JS = r"""() => {
  let summaryHtml = null;
  const sum = document.querySelector(
    "[class*='Resources_Resources__summary'] [class*='Markdown_Markdown']"
  );
  if (sum) summaryHtml = sum.innerHTML;
  const resources = [];
  document.querySelectorAll("[class*='FilesAndLinks'] a[href]").forEach(a => {
    const t = a.querySelector("[class*='Item__title']");
    const title = (t ? t.textContent : a.textContent).trim();
    if (a.href) resources.push({ title, url: a.href });
  });
  return { summaryHtml, resources };
}"""

# Dominios desde los que Platzi sirve archivos descargables propios.
_PLATZI_FILE_HOSTS = ("static.platzi.com", "files.platzi.com")

_MDSTRM_EMBED_RE = re.compile(r"https?://mdstrm\.com/embed/(\w+)")
# .m3u8 que NO sea un playlist de subtítulos (.vtt.m3u8).
_M3U8_RE = re.compile(r"https?://[^\s\"'}]+?(?<!\.vtt)\.m3u8\b")
_VTT_RE = re.compile(r"https?://[^\s\"'}]+\.vtt\b")


class PlatziExtractor(Extractor):
    name = "platzi"

    @staticmethod
    def supports(url: str) -> bool:
        return "platzi.com" in url

    # -- Estructura del curso ------------------------------------------------
    async def list_course(self, ctx: BrowserContext, url: str) -> Course:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            # Esperar a que el temario (renderizado en cliente) esté presente.
            with contextlib.suppress(Exception):
                await page.wait_for_selector(
                    "ul[class*='Materials'] a[class*='ItemLink']", timeout=15000
                )
            raw = await page.evaluate(_SYLLABUS_JS)
            return self._build_course(url, raw)
        finally:
            await page.close()

    def _build_course(self, url: str, raw: dict) -> Course:
        """Construye el ``Course`` a partir del dict extraído por JS en la página."""
        title = (raw.get("title") or "Curso").strip()
        chapters: list[Chapter] = []
        seen: set[str] = set()
        unit_index = 0
        for ci, ch in enumerate(raw.get("chapters", []), start=1):
            units: list[Unit] = []
            for u in ch.get("units", []):
                href = u.get("href") or ""
                if not href or href in seen:
                    continue
                seen.add(href)
                if not href.startswith("http"):
                    href = f"{PLATZI_BASE_URL}{href}"
                unit_index += 1
                units.append(
                    Unit(
                        title=(u.get("title") or f"Clase {unit_index}").strip(),
                        url=href,
                        type=self._classify_unit(
                            href, u.get("thumb", ""), u.get("duration", "")
                        ),
                        index=unit_index,
                    )
                )
            if units:
                chapters.append(
                    Chapter(
                        title=(ch.get("title") or f"Módulo {ci}").strip(),
                        index=len(chapters) + 1,
                        units=units,
                    )
                )
        return Course(title=title, url=url, chapters=chapters)

    @staticmethod
    def _classify_unit(href: str, thumb: str, duration: str) -> UnitType:
        """Clasifica una unidad a partir de señales del temario, sin navegarla.

        Evita abrir clases sin video: una clase de video se delata por su
        miniatura de Mediastream o por el badge de duración del temario; los
        quizzes por la URL; el resto se trata como lectura (``LECTURE``).
        """
        low = href.lower()
        if "/quiz/" in low or "/examen" in low or "/test/" in low:
            return UnitType.QUIZ
        if (thumb and any(host in thumb for host in MEDIASTREAM_HOSTS)) or duration:
            return UnitType.VIDEO
        return UnitType.LECTURE

    # -- Resolución de la fuente de video -----------------------------------
    async def resolve_video(self, ctx: BrowserContext, unit: Unit) -> VideoSource | None:
        if unit.type != UnitType.VIDEO or not unit.url:
            return None

        page = await ctx.new_page()
        embed_urls: list[str] = []
        m3u8_urls: list[str] = []
        vtt_urls: set[str] = set()

        def on_request(req: Request) -> None:
            u = req.url
            if any(host in u for host in MEDIASTREAM_HOSTS) and "/embed/" in u:
                embed_urls.append(u)
            if _M3U8_RE.search(u):
                m3u8_urls.append(u)
            elif _VTT_RE.search(u):
                vtt_urls.add(u)

        page.on("request", on_request)
        try:
            await page.goto(unit.url, wait_until="domcontentloaded")
            # Dar tiempo al reproductor a disparar las peticiones de Mediastream.
            with contextlib.suppress(Exception):
                await page.wait_for_event(
                    "request",
                    predicate=lambda r: any(h in r.url for h in MEDIASTREAM_HOSTS)
                    or bool(_M3U8_RE.search(r.url)),
                    timeout=20000,
                )
            # Margen extra para subtítulos y playlists secundarios.
            await page.wait_for_timeout(1500)

            # Como respaldo, leer el src del iframe del reproductor en el DOM.
            if not embed_urls:
                embed_urls.extend(await self._iframe_embeds(page))
        finally:
            page.remove_listener("request", on_request)
            await page.close()

        raw_cookies = await ctx.cookies()
        cookies = browser.cookies_as_dict(raw_cookies)
        cookie_jar = browser.cookies_as_records(raw_cookies)
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Referer": PLATZI_BASE_URL + "/"}
        subtitles = [Subtitle(url=v) for v in sorted(vtt_urls)]

        # Preferir el embed de Mediastream (yt-dlp lo resuelve con sus tokens).
        if embed_urls:
            return VideoSource(
                url=embed_urls[0],
                is_embed=True,
                http_headers=headers,
                cookies=cookies,
                cookie_jar=cookie_jar,
                subtitles=subtitles,
            )
        if m3u8_urls:
            # El master suele ser el más corto / sin segmentos; tomar el primero.
            return VideoSource(
                url=m3u8_urls[0],
                is_embed=False,
                http_headers=headers,
                cookies=cookies,
                cookie_jar=cookie_jar,
                subtitles=subtitles,
            )
        return None

    @staticmethod
    async def _iframe_embeds(page) -> list[str]:
        found: list[str] = []
        try:
            frames = page.locator("iframe[src*='mdstrm.com']")
            for i in range(await frames.count()):
                src = await frames.nth(i).get_attribute("src")
                if src and _MDSTRM_EMBED_RE.search(src):
                    found.append(src)
        except Exception:
            pass
        return found

    # -- Material complementario (resumen, recursos, snapshot) ---------------
    async def resolve_extras(
        self, ctx: BrowserContext, unit: Unit, *, capture_page: bool = False
    ) -> UnitExtras:
        if not unit.url:
            return UnitExtras()

        page = await ctx.new_page()
        try:
            await page.goto(unit.url, wait_until="domcontentloaded")
            # El panel de recursos se hidrata en cliente tras cargar el DOM, y la
            # lista de archivos/enlaces (FilesAndLinks) aparece después del resumen.
            with contextlib.suppress(Exception):
                await page.wait_for_selector("[class*='Resources_Resources__']", timeout=10000)
            # Esperar la lista de adjuntos si existe; el timeout corto cubre las
            # clases que simplemente no tienen recursos.
            with contextlib.suppress(Exception):
                await page.wait_for_selector("[class*='FilesAndLinks']", timeout=4000)
            raw = await page.evaluate(_EXTRAS_JS)

            mhtml: str | None = None
            if capture_page:
                mhtml = await self._capture_mhtml(ctx, page)
        finally:
            await page.close()

        resources = [
            Resource(title=r.get("title") or "recurso", url=url, kind=self._resource_kind(url))
            for r in raw.get("resources", [])
            if (url := r.get("url"))
        ]
        return UnitExtras(
            summary_html=raw.get("summaryHtml") or None,
            resources=resources,
            page_mhtml=mhtml,
        )

    @staticmethod
    def _resource_kind(url: str) -> ResourceKind:
        """Distingue un archivo alojado por Platzi de un enlace externo."""
        if any(host in url for host in _PLATZI_FILE_HOSTS):
            return ResourceKind.FILE
        return ResourceKind.LINK

    @staticmethod
    async def _capture_mhtml(ctx: BrowserContext, page) -> str | None:
        """Captura un snapshot MHTML de la página vía CDP (solo Chromium)."""
        try:
            client = await ctx.new_cdp_session(page)
            result = await client.send("Page.captureSnapshot", {"format": "mhtml"})
            return result.get("data")
        except Exception:
            return None
