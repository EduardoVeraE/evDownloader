"""Authenticated LinkedIn Learning metadata and progressive media extraction.

Uses the same detailedCourses endpoint as yt-dlp's LinkedIn Learning extractor.
No password login, browser automation, DRM handling, or automatic retry.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

import rnet
from playwright.async_api import BrowserContext

from .. import browser
from ..config import Settings
from ..models import (
    Chapter,
    Cookie,
    Course,
    Subtitle,
    Unit,
    UnitExtras,
    VideoSource,
)
from ..url_policy import URLPolicy
from .base import Extractor

_API = "https://www.linkedin.com/learning-api/detailedCourses"
_SLUG = r"[a-zA-Z0-9][a-zA-Z0-9_-]*"
_PATH = re.compile(rf"/learning/({_SLUG})(?:/({_SLUG}))?/?\Z")
# A real lesson rendition sits well above this floor. LinkedIn answers an
# unpublished lesson with the real audio muxed against a branded animation,
# which lands near 200 kbps regardless of resolution.
_UNPUBLISHED_BITRATE_FLOOR_KBPS = 500.0


class LinkedInLearningError(ValueError):
    """Sanitized authentication, trust, schema, or unavailable-media failure."""


class LinkedInLearningExtractor(Extractor):
    name = "linkedin"
    needs_browser = False
    home_url = "https://www.linkedin.com/learning/"
    url_policy = URLPolicy(name, ("www.linkedin.com", "linkedin.com"))
    resource_host_suffixes = ("linkedin.com", "licdn.com")
    media_policy = URLPolicy("linkedin-media", resource_host_suffixes)

    def __init__(self) -> None:
        self._browser_name: str | None = None
        self._cookies: list[Cookie] | None = None
        self._course_slug: str | None = None
        self._last_video: tuple[str, dict[str, Any]] | None = None

    @classmethod
    def supports(cls, url: str) -> bool:
        if not cls.url_policy.allows(url):
            return False
        parsed = urlsplit(url)
        return bool(
            parsed.hostname in {"linkedin.com", "www.linkedin.com"}
            and _PATH.fullmatch(parsed.path)
            and not parsed.fragment
        )

    def configure(self, settings: Settings) -> None:
        self._browser_name = settings.cookies_from_browser
        self._cookies = None
        self._course_slug = None
        self._last_video = None

    def _cookie_records(self) -> list[Cookie]:
        if self._cookies is None:
            try:
                raw = browser.resolve_cookies(self.name, self._browser_name)
            except Exception:
                raise LinkedInLearningError(
                    "LinkedIn Learning: no se pudo leer el perfil del navegador. "
                    "Cierra Firefox Developer Edition y verifica que su perfil "
                    "esté disponible para el lector de cookies de firefox."
                ) from None
            # LinkedIn wraps its CSRF cookie in quotes. Normalize only that
            # known wrapper before shared cookie validation/header scoping.
            normalized = []
            for cookie in raw:
                item = dict(cookie)
                value = item.get("value")
                if (
                    item.get("name") == "JSESSIONID"
                    and isinstance(value, str)
                    and value.startswith('"')
                    and value.endswith('"')
                ):
                    item["value"] = value[1:-1]
                normalized.append(item)
            try:
                self._cookies = browser.filter_cookie_records(
                    browser.cookies_as_records(normalized), ("linkedin.com",)
                )
            except Exception:
                raise LinkedInLearningError(
                    "LinkedIn Learning: cookies del navegador no compatibles."
                ) from None
        return self._cookies

    def _headers(self, url: str) -> dict[str, str]:
        scoped = browser.cookie_header_for_url(
            self._cookie_records(), url, trusted_host_suffixes=("linkedin.com",)
        )
        values = dict(part.split("=", 1) for part in scoped.split("; ")) if scoped else {}
        if not values.get("li_at") or not values.get("JSESSIONID"):
            raise LinkedInLearningError(
                "LinkedIn Learning requiere una sesión vigente. Abre el curso en Firefox "
                "Developer Edition e indica --cookies-from-browser firefox."
            )
        return {
            "Cookie": scoped or "",
            "Csrf-Token": values["JSESSIONID"],
            "Referer": self.home_url,
        }

    async def _api(self, course: str, fields: str, video: str | None = None) -> dict[str, Any]:
        query = {"courseSlug": course, "fields": fields, "q": "slugs"}
        if video:
            query.update(videoSlug=video, resolution="_720")
        url = f"{_API}?{urlencode(query)}"
        headers = self._headers(url)
        client = rnet.Client(timeout=30, connect_timeout=10)
        try:
            response = await client.get(url, headers=headers, allow_redirects=False)
            try:
                status = response.status_code.as_int()
                if status in {401, 403, 999} or 300 <= status < 400:
                    raise LinkedInLearningError(
                        "LinkedIn Learning rechazó la sesión o el acceso. Abre el curso "
                        "en Firefox Developer Edition y completa el login o desafío "
                        "antes de reintentar."
                    )
                if status != 200:
                    raise LinkedInLearningError(f"LinkedIn Learning API: HTTP {status}.")
                text = await response.text()
                if len(text) > 10 * 1024 * 1024:
                    raise LinkedInLearningError("LinkedIn Learning: respuesta demasiado grande.")
                payload = json.loads(text)
            finally:
                with contextlib.suppress(Exception):
                    await response.close()
        except LinkedInLearningError:
            raise
        except Exception:
            raise LinkedInLearningError(
                "LinkedIn Learning: error de conexión o respuesta no compatible."
            ) from None
        if not isinstance(payload, dict):
            raise LinkedInLearningError("LinkedIn Learning: esquema de curso no compatible.")
        elements = payload.get("elements")
        if (
            not isinstance(elements, list)
            or len(elements) != 1
            or not isinstance(elements[0], dict)
        ):
            raise LinkedInLearningError(
                "LinkedIn Learning: curso no disponible o esquema no compatible."
            )
        return elements[0]

    @staticmethod
    def _text(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise LinkedInLearningError("LinkedIn Learning: metadata obligatoria ausente.")
        return value.strip()

    async def list_course(self, ctx: BrowserContext | None, url: str) -> Course:
        if not self.supports(url):
            raise LinkedInLearningError("LinkedIn Learning: URL de curso no válida.")
        match = _PATH.fullmatch(urlsplit(url).path)
        assert match is not None
        slug = match[1]
        data = await self._api(slug, "chapters,description,title")
        raw_chapters = data.get("chapters")
        if not isinstance(raw_chapters, list) or not raw_chapters:
            raise LinkedInLearningError("LinkedIn Learning: capítulos no disponibles.")
        chapters: list[Chapter] = []
        seen: set[str] = set()
        for index, chapter in enumerate(raw_chapters, 1):
            if not isinstance(chapter, dict) or not isinstance(chapter.get("videos"), list):
                raise LinkedInLearningError(
                    "LinkedIn Learning: esquema de capítulos no compatible."
                )
            units = []
            for video in chapter["videos"]:
                if not isinstance(video, dict):
                    raise LinkedInLearningError(
                        "LinkedIn Learning: esquema de lección no compatible."
                    )
                video_slug = self._text(video.get("slug"))
                if not re.fullmatch(_SLUG, video_slug) or video_slug in seen:
                    raise LinkedInLearningError(
                        "LinkedIn Learning: identificador de lección inválido."
                    )
                seen.add(video_slug)
                units.append(
                    Unit(
                        title=self._text(video.get("title")),
                        url=f"{self.home_url}{slug}/{video_slug}",
                        index=len(seen),
                    )
                )
            chapters.append(
                Chapter(title=self._text(chapter.get("title")), index=index, units=units)
            )
        if not seen:
            raise LinkedInLearningError("LinkedIn Learning: curso sin lecciones disponibles.")
        self._course_slug = slug
        return Course(
            title=self._text(data.get("title")), url=f"{self.home_url}{slug}", chapters=chapters
        )

    async def _video(self, unit: Unit) -> dict[str, Any]:
        if not self.supports(unit.url):
            raise LinkedInLearningError("LinkedIn Learning: URL de lección no válida.")
        match = _PATH.fullmatch(urlsplit(unit.url).path)
        assert match is not None
        if not match[2] or self._course_slug is not None and match[1] != self._course_slug:
            raise LinkedInLearningError("LinkedIn Learning: lección fuera del curso seleccionado.")
        if self._last_video is not None and self._last_video[0] == unit.url:
            return self._last_video[1]
        data = (await self._api(match[1], "selectedVideo", match[2])).get("selectedVideo")
        if not isinstance(data, dict):
            raise LinkedInLearningError("LinkedIn Learning: lección no disponible.")
        self._last_video = (unit.url, data)
        return data

    @staticmethod
    def _subtitles(data: dict[str, Any]) -> list[Subtitle]:
        transcript = data.get("transcript")
        if transcript is None:
            return []
        if not isinstance(transcript, dict) or not isinstance(transcript.get("lines"), list):
            raise LinkedInLearningError("LinkedIn Learning: transcripción no compatible.")
        lines = transcript["lines"]
        if not lines:
            return []
        cues: list[tuple[int, str]] = []
        for line in lines:
            if not isinstance(line, dict):
                raise LinkedInLearningError("LinkedIn Learning: transcripción no compatible.")
            start, caption = line.get("transcriptStartAt"), line.get("caption")
            if (
                type(start) is not int
                or start < 0
                or not isinstance(caption, str)
                or not caption.strip()
                or cues
                and start <= cues[-1][0]
            ):
                raise LinkedInLearningError("LinkedIn Learning: transcripción inválida.")
            cues.append((start, caption.replace("-->", "→").strip()))
        duration = data.get("durationInSeconds")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or duration * 1000 <= cues[-1][0]
        ):
            raise LinkedInLearningError("LinkedIn Learning: duración de transcripción inválida.")

        def timestamp(ms: int) -> str:
            seconds, millis = divmod(ms, 1000)
            minutes, seconds = divmod(seconds, 60)
            hours, minutes = divmod(minutes, 60)
            return f"{hours:02}:{minutes:02}:{seconds:02}.{millis:03}"

        content = "WEBVTT\n\n"
        for index, (start, caption) in enumerate(cues):
            end = cues[index + 1][0] if index + 1 < len(cues) else int(duration * 1000)
            content += f"{timestamp(start)} --> {timestamp(end)}\n{caption}\n\n"
        # Do not guess English for a localized course when the API omits language.
        language = transcript.get("language")
        lang = (
            language
            if isinstance(language, str) and re.fullmatch(r"[a-zA-Z_-]{2,20}", language)
            else "und"
        )
        return [Subtitle(lang=lang, url="", content=content)]

    async def _progressive_bitrate_kbps(self, media: str, duration: float) -> float | None:
        """Return the progressive rendition bitrate, or None when it is unmeasurable."""
        if duration <= 0:
            return None
        client = rnet.Client(timeout=15, connect_timeout=5)
        try:
            response = await client.head(media, allow_redirects=True)
            try:
                if response.status_code.as_int() != 200:
                    return None
                length = response.headers.get("content-length")
            finally:
                with contextlib.suppress(Exception):
                    await response.close()
        except Exception:
            return None
        text: str | None = length.decode("ascii", "ignore") if isinstance(length, bytes) else length
        try:
            size = int(text) if text is not None else 0
        except TypeError, ValueError:
            return None
        if size <= 0:
            return None
        return size * 8 / duration / 1000

    async def _reject_unpublished_media(self, data: dict[str, Any], media: str) -> None:
        """Fail loudly when LinkedIn serves an audio-plus-filler rendition.

        Lessons flagged ``public: false`` or ``androidPublic: false`` come back
        with the real audio track muxed against a branded animation instead of
        the lesson video. The measured bitrate is the independent confirmation:
        a real rendition sits far above the floor, while the filler lands near
        200 kbps. Unmeasurable size never blocks a download.
        """
        public = data.get("public")
        android_public = data.get("androidPublic")
        if public is not False and android_public is not False:
            return
        raw_duration = data.get("durationInSeconds")
        duration = (
            float(raw_duration)
            if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool)
            else 0.0
        )
        kbps = await self._progressive_bitrate_kbps(media, duration)
        if kbps is None or kbps >= _UNPUBLISHED_BITRATE_FLOOR_KBPS:
            return
        size_mb = kbps * duration / 8 / 1000
        course = self._course_slug or "desconocido"
        raise LinkedInLearningError(
            "LinkedIn Learning: LinkedIn no está publicando el video real de esta "
            f"lección. El curso '{course}' trae el video marcado como no público "
            f"(public={public!r}, androidPublic={android_public!r}) y la API solo "
            "entrega una rendición de audio con una animación de relleno "
            f"({kbps:.0f} kbps, {size_mb:.1f} MB para {duration:.0f}s). "
            "No es un fallo de evd, de las cookies ni de DRM: el mismo curso muestra "
            "solo audio en el navegador. Comprueba si el curso sigue publicado y, si "
            "debería estarlo, repórtalo a soporte de LinkedIn Learning indicando que "
            "la API devuelve public=false y androidPublic=false."
        )

    async def resolve_video(self, ctx: BrowserContext | None, unit: Unit) -> VideoSource:
        data = await self._video(unit)
        if any(data.get(key) for key in ("drm", "drmProtected", "isDrmProtected")):
            raise LinkedInLearningError(
                "LinkedIn Learning: DRM no compatible; no se intentó descifrado."
            )
        urls = data.get("url")
        media = urls.get("progressiveUrl") if isinstance(urls, dict) else None
        if not isinstance(media, str) or not self.media_policy.allows(media):
            raise LinkedInLearningError(
                "LinkedIn Learning: video progresivo autorizado no disponible."
            )
        await self._reject_unpublished_media(data, media)
        return VideoSource(
            url=media,
            http_headers={"Referer": self.home_url},
            cookie_jar=self._cookie_records(),
            trusted_host_suffixes=self.resource_host_suffixes,
            subtitles=self._subtitles(data),
            subtitle_inventory_confirmed="transcript" in data,
            subtitle_inventory_required=True,
        )

    async def resolve_extras(
        self, ctx: BrowserContext | None, unit: Unit, *, capture_page: bool = False
    ) -> UnitExtras:
        # Absent selectedVideo fields cannot establish a resource inventory.
        # Never report empty success for an unsupported operation.
        raise LinkedInLearningError(
            "LinkedIn Learning: inventario de recursos no compatible. "
            "Usa --no-resources para descargar solo video y subtítulos."
        )
