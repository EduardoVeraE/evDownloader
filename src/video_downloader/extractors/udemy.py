"""Extractor de Udemy.

Estrategia (misma filosofía que el extractor de Platzi):

* **Estructura del curso**: se extrae el temario del DOM renderizado con
  selectores por subcadena (resistentes a los hashes de clases de Udemy).
* **Fuente de video**: se **intercepta la red** mientras carga el reproductor
  para capturar el master ``.m3u8`` (HLS, sin DRM) y los subtítulos ``.vtt``.

* **DRM**: muchos cursos de pago usan DRM Widevine y entregan manifiestos DASH
  ``.mpd`` cifrados + una petición de licencia en vez de HLS ``.m3u8``. Esos NO
  son descargables sin claves CDM: se detectan por red y se omiten con aviso.

Nota: los selectores del temario dependen del marcado de Udemy y deben
verificarse en vivo (como se hizo con Platzi). Se usan varias alternativas por
subcadena para tolerar cambios menores.
"""

from __future__ import annotations

import contextlib
import re

from playwright.async_api import BrowserContext, Request
from rich.console import Console

from .. import browser
from ..config import DEFAULT_USER_AGENT, UDEMY_BASE_URL, UDEMY_LOGIN_URL
from ..models import Chapter, Course, Subtitle, Unit, UnitType, VideoSource
from .base import Extractor

console = Console()

# Extrae el temario del DOM renderizado. Cada sección del currículo lleva un
# título y una lista de ítems (clase/lectura/quiz). Se capturan señales para
# clasificar el tipo sin navegar cada ítem: el icono de tipo (``udi-video`` /
# ``udi-article`` / ``udi-quiz``) y el badge de duración.
_CURRICULUM_JS = r"""() => {
  const courseTitle = (
    document.querySelector('[data-purpose="lead-title"]')?.textContent ||
    document.querySelector('h1')?.textContent || ''
  ).trim();
  const chapters = [];
  const sections = document.querySelectorAll(
    "[class*='section--section'], [data-purpose='curriculum-section-container']"
  );
  sections.forEach(sec => {
    const h = sec.querySelector(
      "[class*='section--title'], [data-purpose='section-heading'] span, h3"
    );
    const chTitle = h ? h.textContent.trim() : '';
    const units = [];
    sec.querySelectorAll(
      "a[href*='/learn/lecture/'], a[href*='/learn/quiz/'], [class*='curriculum-item-link'] a"
    ).forEach(a => {
      const href = a.getAttribute('href');
      if (!href) return;
      const t = a.querySelector("[class*='item-link--title'], [class*='title']");
      const title = (t ? t.textContent : a.textContent).trim();
      const icon = a.querySelector("[class*='udi']");
      const kind = icon ? (icon.getAttribute('class') || '') : '';
      const durEl = a.querySelector("[class*='content-summary'], [class*='duration']");
      const duration = durEl ? durEl.textContent.trim() : '';
      units.push({ href, title, kind, duration });
    });
    if (units.length) chapters.push({ title: chTitle, units });
  });
  return { title: courseTitle, chapters };
}"""

# .m3u8 que NO sea un playlist de subtítulos (.vtt.m3u8).
_M3U8_RE = re.compile(r"https?://[^\s\"'}]+?(?<!\.vtt)\.m3u8\b")
_MPD_RE = re.compile(r"https?://[^\s\"'}]+\.mpd\b")
_VTT_RE = re.compile(r"https?://[^\s\"'}]+\.vtt\b")
# Señales en la URL de una petición de licencia DRM (Widevine).
_DRM_URL_SIGNALS = ("widevine", "/media-license", "media-drm", "/license")
# Detecta un patrón de tiempo (mm:ss) en el badge de duración.
_TIME_RE = re.compile(r"\d{1,2}:\d{2}")


class UdemyExtractor(Extractor):
    name = "udemy"
    login_url = UDEMY_LOGIN_URL
    home_url = UDEMY_BASE_URL
    # El menú/avatar de usuario solo aparece autenticado. (Verificar en vivo.)
    auth_ready_selector = (
        "[data-purpose='user-dropdown'], header a[href*='/user/'], "
        "[class*='user-avatar']"
    )

    @staticmethod
    def supports(url: str) -> bool:
        return "udemy.com" in url

    # -- Estructura del curso ------------------------------------------------
    async def list_course(self, ctx: BrowserContext, url: str) -> Course:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            with contextlib.suppress(Exception):
                await page.wait_for_selector(
                    "a[href*='/learn/lecture/'], [class*='curriculum-item-link']",
                    timeout=15000,
                )
            raw = await page.evaluate(_CURRICULUM_JS)
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
                    href = f"{UDEMY_BASE_URL}{href}"
                unit_index += 1
                units.append(
                    Unit(
                        title=(u.get("title") or f"Clase {unit_index}").strip(),
                        url=href,
                        type=self._classify_unit(
                            href, u.get("kind", ""), u.get("duration", "")
                        ),
                        index=unit_index,
                    )
                )
            if units:
                chapters.append(
                    Chapter(
                        title=(ch.get("title") or f"Sección {ci}").strip(),
                        index=len(chapters) + 1,
                        units=units,
                    )
                )
        return Course(title=title, url=url, chapters=chapters)

    @staticmethod
    def _classify_unit(href: str, kind: str, duration: str) -> UnitType:
        """Clasifica una unidad por señales del temario, sin navegarla.

        Prioriza el icono de tipo de Udemy (``udi-video`` / ``udi-article`` /
        ``udi-quiz``); si no lo hay, usa la URL y el badge de duración.
        """
        low = href.lower()
        klow = kind.lower()
        if "/quiz/" in low or "quiz" in klow:
            return UnitType.QUIZ
        if "article" in klow:
            return UnitType.LECTURE
        if "video" in klow or _TIME_RE.search(duration):
            return UnitType.VIDEO
        return UnitType.LECTURE

    # -- Resolución de la fuente de video -----------------------------------
    async def resolve_video(self, ctx: BrowserContext, unit: Unit) -> VideoSource | None:
        if unit.type != UnitType.VIDEO or not unit.url:
            return None

        page = await ctx.new_page()
        m3u8_urls: list[str] = []
        vtt_urls: set[str] = set()
        drm_seen = False

        def on_request(req: Request) -> None:
            nonlocal drm_seen
            u = req.url
            if _M3U8_RE.search(u):
                m3u8_urls.append(u)
            elif _VTT_RE.search(u):
                vtt_urls.add(u)
            if _MPD_RE.search(u) or any(sig in u.lower() for sig in _DRM_URL_SIGNALS):
                drm_seen = True

        page.on("request", on_request)
        try:
            await page.goto(unit.url, wait_until="domcontentloaded")
            # Esperar a que el reproductor dispare el manifiesto (HLS o DASH/DRM).
            with contextlib.suppress(Exception):
                await page.wait_for_event(
                    "request",
                    predicate=lambda r: bool(_M3U8_RE.search(r.url))
                    or bool(_MPD_RE.search(r.url))
                    or any(sig in r.url.lower() for sig in _DRM_URL_SIGNALS),
                    timeout=20000,
                )
            # Margen extra para subtítulos y playlists secundarios.
            await page.wait_for_timeout(1500)
        finally:
            page.remove_listener("request", on_request)
            await page.close()

        # DRM Widevine: hay manifiesto cifrado/licencia y ningún HLS descargable.
        if drm_seen and not m3u8_urls:
            console.print(
                f"[yellow]DRM (Widevine) detectado — clase omitida:[/yellow] {unit.title}"
            )
            return None

        if not m3u8_urls:
            return None

        raw_cookies = await ctx.cookies()
        return VideoSource(
            url=m3u8_urls[0],
            is_embed=False,
            http_headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": UDEMY_BASE_URL + "/"},
            cookies=browser.cookies_as_dict(raw_cookies),
            cookie_jar=browser.cookies_as_records(raw_cookies),
            subtitles=[Subtitle(url=v) for v in sorted(vtt_urls)],
        )
