"""Authenticated SharePoint folder enumeration and trusted video resolution."""

from __future__ import annotations

import contextlib
import json
import os
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import rnet
from playwright.async_api import BrowserContext

from .. import browser
from ..config import CDMS_DIR, DEFAULT_USER_AGENT, RNET_IMPERSONATE, Settings
from ..drm import detect_drm
from ..models import Chapter, Cookie, Course, DrmInfo, Unit, UnitType, VideoSource
from ..url_policy import URLPolicy, parse_url_authority
from .base import Extractor

_MAX_REDIRECTS = 3
_VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})
_LOGIN_MARKERS = ("login.microsoftonline.com", "sign in", "iniciar sesión", "signin")
_API_SELECT = "Name,ServerRelativeUrl,TimeLastModified,ListItemAllFields"


class SharePointTrustError(ValueError):
    """The URL or redirect crossed the SharePoint trust boundary."""


class SharePointAuthError(ValueError):
    """SharePoint rejected the current cookie-authenticated request."""


class SharePointResponseError(ValueError):
    """SharePoint returned malformed or unsupported REST data."""


class SharePointEmptyError(ValueError):
    """The folder contains no supported recordings."""


class SharePointDrmError(RuntimeError):
    """The recording uses DRM that evdownloader cannot decrypt."""


class SharePointExtractor(Extractor):
    name = "sharepoint"
    needs_browser = False
    home_url = "https://www.sharepoint.com/"
    resource_host_suffixes = ("sharepoint.com",)
    url_policy = URLPolicy(name, ("sharepoint.com",))

    def __init__(self) -> None:
        self._cookies_from_browser: str | None = None
        self._cookie_jar: list[Cookie] | None = None
        self._settings = Settings()
        self._client: rnet.Client | None = None

    @classmethod
    def supports(cls, url: str) -> bool:
        if not cls.url_policy.allows(url):
            return False
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False
        path = unquote(parsed.path).lower()
        return "stream.aspx" not in path and not parsed.query and not parsed.fragment

    def configure(self, settings: Settings) -> None:
        browser_name = settings.cookies_from_browser
        if browser_name and browser_name.lower() not in {"chrome", "edge"}:
            raise ValueError("SharePoint v1 solo admite cookies de Chrome or Edge.")
        self._settings = settings
        self._cookies_from_browser = browser_name.lower() if browser_name else None
        self._cookie_jar = None

    @staticmethod
    def _folder_path(folder_url: str) -> str:
        authority = parse_url_authority(folder_url)
        if authority is None:
            raise SharePointTrustError("SharePoint requiere una URL HTTPS de folder válida.")
        parsed = urlsplit(folder_url)
        path = unquote(parsed.path)
        if not path or path == "/" or parsed.query or parsed.fragment:
            raise ValueError("SharePoint solo acepta URLs de folder sin query.")
        if "stream.aspx" in path.lower() or Path(path).suffix.lower() in _VIDEO_EXTENSIONS:
            raise ValueError("SharePoint requiere una URL de folder, no un archivo de video.")
        if (
            not path.startswith("/")
            or "\x00" in path
            or any(segment in {".", ".."} for segment in path.split("/"))
        ):
            raise SharePointResponseError("La ruta de folder de SharePoint es inválida.")
        return path

    async def list_course(self, ctx: BrowserContext | None, url: str) -> Course:
        path = self._folder_path(url)
        if not self.url_policy.allows(url):
            raise SharePointTrustError("SharePoint rechazó una URL de folder no confiable.")
        if not self._cookie_records():
            raise SharePointAuthError("SharePoint requiere cookies autenticadas de Chrome or Edge.")
        files = await self._fetch_files(url, path)
        recordings = self._recordings(url, files)
        if not recordings:
            raise SharePointEmptyError("SharePoint no encontró recordings en el folder.")

        groups: dict[str, list[dict[str, str]]] = {}
        for item in recordings:
            groups.setdefault(item["normalized_title"], []).append(item)

        chapters: list[Chapter] = []
        unit_index = 0
        for chapter_index, key in enumerate(sorted(groups), 1):
            items = sorted(groups[key], key=lambda item: item["url"])
            units: list[Unit] = []
            for item in items:
                unit_index += 1
                units.append(
                    Unit(
                        title=item["title"],
                        url=item["url"],
                        type=UnitType.VIDEO,
                        index=unit_index,
                    )
                )
            chapters.append(Chapter(title=items[0]["title"], index=chapter_index, units=units))

        folder_name = unquote(path.rstrip("/").rsplit("/", 1)[-1]) or "SharePoint"
        return Course(title=folder_name, url=url, chapters=chapters)

    async def resolve_video(self, ctx: BrowserContext | None, unit: Unit) -> VideoSource | None:
        if unit.type is not UnitType.VIDEO or not unit.url:
            return None
        if not self.url_policy.allows(unit.url):
            raise SharePointTrustError("SharePoint rechazó una source fuera del proveedor.")
        cookies = self._cookie_records()
        if not cookies:
            raise SharePointAuthError("SharePoint requiere cookies antes de resolver video.")
        drm = await self._probe_drm(unit.url)
        if drm is not None:
            scheme = drm.scheme.casefold()
            if scheme != "widevine":
                raise SharePointDrmError(
                    f"SharePoint devolvió DRM {drm.scheme}; se conserva el encrypted container "
                    "pero evdownloader no ofrece decryption para PlayReady o AES."
                )
            device = self._select_cdm()
            if device is None:
                raise SharePointDrmError(
                    "SharePoint Widevine requiere un archivo .wvd válido en la carpeta CDMs."
                )
            self._settings.drm_device = device

        return VideoSource(
            url=unit.url,
            is_embed="stream.aspx" in urlsplit(unit.url).path.lower(),
            http_headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": self.home_url},
            cookie_jar=cookies,
            trusted_host_suffixes=self.url_policy.host_suffixes,
            drm=drm,
        )

    @staticmethod
    def _normalize_title(title: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", title).casefold().split())

    @classmethod
    def _recordings(cls, folder_url: str, files: list[dict[str, Any]]) -> list[dict[str, str]]:
        authority = parse_url_authority(folder_url)
        assert authority is not None
        folder_path = unquote(urlsplit(folder_url).path).rstrip("/") or "/"
        recordings: list[dict[str, str]] = []
        for item in files:
            name = item.get("Name")
            relative = item.get("ServerRelativeUrl")
            if not isinstance(name, str) or not isinstance(relative, str):
                continue
            relative_url = urlsplit(relative)
            path = unquote(relative_url.path)
            if (
                not path.startswith("/")
                or not path
                or "\x00" in path
                or relative_url.query
                or relative_url.fragment
                or any(segment in {".", ".."} for segment in path.split("/"))
                or (folder_path != "/" and not path.startswith(f"{folder_path}/"))
            ):
                raise SharePointResponseError("SharePoint devolvió un file path inválido.")
            if Path(path).suffix.casefold() not in _VIDEO_EXTENSIONS:
                continue
            fields = item.get("ListItemAllFields")
            raw_title = fields.get("Title") if isinstance(fields, dict) else None
            title = (
                raw_title.strip()
                if isinstance(raw_title, str) and raw_title.strip()
                else Path(name).stem
            )
            title = " ".join(title.split()) or Path(name).stem
            recordings.append(
                {
                    "title": title,
                    "normalized_title": cls._normalize_title(title),
                    "url": f"https://{authority.hostname}{quote(path, safe='/@:+')}",
                }
            )
        return recordings

    async def _fetch_files(self, folder_url: str, folder_path: str) -> list[dict[str, Any]]:
        authority = parse_url_authority(folder_url)
        assert authority is not None
        current = (
            f"https://{authority.hostname}/_api/web/GetFolderByServerRelativeUrl("
            f"'{quote(folder_path, safe='/')}')/Files?$select={_API_SELECT}"
            "&$expand=ListItemAllFields"
        )
        visited: set[str] = set()
        files: list[dict[str, Any]] = []
        for hop in range(_MAX_REDIRECTS + 1):
            if current in visited:
                raise SharePointTrustError("SharePoint devolvió un ciclo de pagination.")
            visited.add(current)
            if not self.url_policy.allows(current):
                raise SharePointTrustError(
                    "SharePoint rechazó un redirect o pagination fuera del proveedor."
                )
            response = await self._rnet_client().get(
                current,
                headers=self._request_headers(current, folder_url),
                allow_redirects=False,
            )
            try:
                status = response.status_code.as_int()
                location = self._header(response, "location")
                if status in {301, 302, 303, 307, 308}:
                    if not location or hop == _MAX_REDIRECTS:
                        raise SharePointTrustError("SharePoint devolvió un redirect inválido.")
                    current = urljoin(current, location)
                    continue
                if status in {401, 403}:
                    raise SharePointAuthError(
                        "SharePoint rechazó las cookies o los permisos del folder."
                    )
                text = await response.text()
                if self._looks_like_login(text):
                    raise SharePointAuthError("SharePoint redirigió a una página de login.")
                if status < 200 or status >= 300:
                    raise SharePointResponseError(f"SharePoint REST devolvió HTTP {status}.")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise SharePointResponseError(
                        "SharePoint REST devolvió JSON inválido."
                    ) from exc
                if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
                    raise SharePointResponseError(
                        "SharePoint REST no devolvió una lista de Files."
                    )
                files.extend(item for item in payload["value"] if isinstance(item, dict))
                next_link = payload.get("@odata.nextLink")
                if next_link is None:
                    return files
                if not isinstance(next_link, str) or not next_link:
                    raise SharePointResponseError("SharePoint devolvió una pagination inválida.")
                current = urljoin(current, next_link)
            finally:
                with contextlib.suppress(Exception):
                    await response.close()
        raise SharePointTrustError("SharePoint excedió el límite de redirects.")

    async def _probe_drm(self, url: str) -> DrmInfo | None:
        """Inspect manifests and provider metadata before handing media to a downloader."""
        path = urlsplit(url).path.casefold()
        is_manifest = path.endswith((".mpd", ".m3u8"))
        current = url
        visited: set[str] = set()
        for hop in range(_MAX_REDIRECTS + 1):
            if current in visited or not self.url_policy.allows(current):
                raise SharePointTrustError("SharePoint rechazó un redirect de manifest.")
            visited.add(current)
            request_headers = self._request_headers(current, self.home_url)
            if not is_manifest:
                request_headers["Range"] = "bytes=0-0"
            response = await self._rnet_client().get(
                current,
                headers=request_headers,
                allow_redirects=False,
            )
            try:
                status = response.status_code.as_int()
                if status in {301, 302, 303, 307, 308}:
                    location = self._header(response, "location")
                    if not location or hop == _MAX_REDIRECTS:
                        raise SharePointTrustError(
                            "SharePoint devolvió un redirect de manifest inválido."
                        )
                    current = urljoin(current, location)
                    continue
                if status < 200 or status >= 300:
                    return None
                try:
                    text = await response.text()
                except Exception:  # noqa: BLE001
                    text = ""
                content_type = self._header(response, "content-type")
                if is_manifest or content_type and any(
                    marker in content_type.casefold()
                    for marker in ("dash", "mpd", "mpegurl", "m3u8")
                ):
                    result = detect_drm(text, url=current, content_type=content_type)
                    if result.systems:
                        return result.systems[0]
                return self._drm_from_response(response, text)
            finally:
                with contextlib.suppress(Exception):
                    await response.close()
        raise SharePointTrustError("SharePoint excedió el límite de redirects del manifest.")

    @staticmethod
    def _drm_from_response(response: Any, text: str) -> DrmInfo | None:
        """Extract an explicit DRM marker from direct-file response metadata."""
        for header_name in (
            "x-drm-system",
            "x-ms-drm-system",
            "x-content-protection",
            "content-protection",
            "x-encrypted",
        ):
            value = SharePointExtractor._header(response, header_name)
            if not value:
                continue
            scheme = SharePointExtractor._drm_scheme(value)
            if scheme is not None:
                return DrmInfo(scheme=scheme)

        if not text.lstrip().startswith(("{", "[")):
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        marker = payload.get("drm") or payload.get("drmSystem") or payload.get("protection")
        if isinstance(marker, dict):
            marker = marker.get("scheme") or marker.get("system") or marker.get("type")
        if not isinstance(marker, str):
            return None
        scheme = SharePointExtractor._drm_scheme(marker)
        return DrmInfo(scheme=scheme) if scheme is not None else None

    @staticmethod
    def _drm_scheme(value: str) -> str | None:
        normalized = value.casefold().strip()
        if normalized in {"", "0", "false", "no", "none", "clear"}:
            return None
        for marker in ("widevine", "playready", "fairplay", "aes", "cenc"):
            if marker in normalized:
                return marker
        if normalized in {"1", "true", "yes", "encrypted"}:
            return "cenc"
        return normalized

    def _select_cdm(self) -> Path | None:
        explicit = self._settings.drm_device
        if explicit is not None:
            return explicit
        directory = self._settings.cdms_dir or CDMS_DIR
        try:
            candidates = sorted(
                candidate
                for candidate in directory.iterdir()
                if candidate.suffix.casefold() == ".wvd"
            )
        except OSError:
            return None
        for candidate in candidates:
            try:
                if (
                    candidate.is_file()
                    and os.access(candidate, os.R_OK)
                    and candidate.stat().st_size
                    and self._is_valid_cdm(candidate)
                ):
                    return candidate
            except OSError:
                continue
        return None

    @staticmethod
    def _is_valid_cdm(path: Path) -> bool:
        try:
            from pywidevine.device import Device

            Device.load(path)
        except Exception:
            return False
        return True

    def _cookie_records(self) -> list[Cookie]:
        if self._cookie_jar is None:
            if self._cookies_from_browser:
                raw = browser.load_browser_cookies(self._cookies_from_browser)
            else:
                raw = browser.load_cookies(self.name)
            self._cookie_jar = browser.cookies_as_records(browser.filter_cookies(self.name, raw))
        return browser.filter_cookie_records(self._cookie_jar, self.url_policy.host_suffixes)

    def _request_headers(self, url: str, referer: str) -> dict[str, str]:
        headers = {"Accept": "application/json;odata=nometadata", "User-Agent": DEFAULT_USER_AGENT}
        if referer:
            headers["Referer"] = referer
        cookie = browser.cookie_header_for_url(
            self._cookie_records(), url, trusted_host_suffixes=self.url_policy.host_suffixes
        )
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _rnet_client(self) -> rnet.Client:
        if self._client is None:
            self._client = rnet.Client(
                impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None)
            )
        return self._client

    @staticmethod
    def _header(response: Any, name: str) -> str | None:
        headers = getattr(response, "headers", {})
        normalized_name = name.casefold()
        for header_name, value in headers.items():
            if isinstance(header_name, str) and header_name.casefold() == normalized_name:
                return value if isinstance(value, str) else None
        return None

    @staticmethod
    def _looks_like_login(text: str) -> bool:
        lowered = text[:20_000].casefold()
        return "<html" in lowered and any(marker in lowered for marker in _LOGIN_MARKERS)
