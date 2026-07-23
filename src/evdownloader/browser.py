"""Gestión del navegador Playwright y persistencia de sesión (cookies).

Centraliza la creación del contexto de navegación con identidad coherente
(mismo User-Agent que luego usa el downloader) y la carga/guardado de cookies.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import math
import os
import tempfile
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, async_playwright

from .config import DEFAULT_USER_AGENT, ensure_dirs, session_file
from .models import Cookie

_SESSION_COOKIE_NAMES: dict[str, frozenset[str]] = {
    "udemy": frozenset({"access_token"}),
    "codigofacilito": frozenset({"_session_id", "_codigofacilito_session"}),
}
_UDEMY_COOKIE_DOMAINS = frozenset({"udemy.com", ".udemy.com", "www.udemy.com"})


def is_udemy_cookie(cookie: Mapping[str, Any]) -> bool:
    """Indica si una cookie pertenece a los dominios permitidos de Udemy."""
    domain = cookie.get("domain")
    return isinstance(domain, str) and domain.lower() in _UDEMY_COOKIE_DOMAINS


def filter_cookies(platform: str, cookies: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Devuelve solo cookies permitidas para la plataforma."""
    if platform != "udemy":
        return [dict(cookie) for cookie in cookies]
    return [dict(cookie) for cookie in cookies if is_udemy_cookie(cookie)]


def load_cookies(platform: str) -> list[dict[str, Any]]:
    """Lee las cookies persistidas de la plataforma, o lista vacía."""
    path = session_file(platform)
    if not path.exists():
        return []
    try:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        cookies = data.get("cookies", [])
        if not isinstance(cookies, list):
            return []
        return filter_cookies(platform, [cookie for cookie in cookies if isinstance(cookie, dict)])
    except json.JSONDecodeError, OSError:
        return []


def save_cookies(cookies: Sequence[Mapping[str, Any]], platform: str) -> None:
    """Persiste las cookies de la plataforma de forma atómica y privada."""
    ensure_dirs()
    path = session_file(platform)
    payload = json.dumps(
        {"cookies": filter_cookies(platform, cookies)}, ensure_ascii=False, indent=2
    )
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def is_cookie_usable(cookie: Mapping[str, Any], *, now: float | None = None) -> bool:
    """Indica si una cookie tiene valor y no ha expirado localmente."""
    name = cookie.get("name")
    value = cookie.get("value")
    if not isinstance(name, str) or not name:
        return False
    if not isinstance(value, str) or not value.strip():
        return False

    expires = cookie.get("expires", 0)
    if expires is None or expires == 0:
        return True
    try:
        expiration = float(expires)
    except TypeError, ValueError:
        return False
    return expiration > (time.time() if now is None else now)


def has_usable_session(platform: str, cookies: Sequence[Mapping[str, Any]]) -> bool:
    """Valida cookies de sesión sin contactar la plataforma."""
    known_names = _SESSION_COOKIE_NAMES.get(platform)
    return any(
        is_cookie_usable(cookie)
        and (platform != "udemy" or is_udemy_cookie(cookie))
        and (known_names is None or cookie.get("name") in known_names)
        for cookie in cookies
    )


def load_browser_cookies(browser_name: str) -> list[dict[str, Any]]:
    """Extrae cookies del navegador configurado sin devolver errores sensibles."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser

        jar = extract_cookies_from_browser(browser_name)
    except Exception:
        raise ValueError(
            f"No se pudieron leer las cookies de {browser_name}. "
            "Verifica que el navegador esté instalado y cerrado."
        ) from None

    return [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
            "expires": cookie.expires or 0,
        }
        for cookie in jar
    ]


def resolve_cookies(platform: str, browser_name: str | None = None) -> list[dict[str, Any]]:
    """Usa la sesión persistida y cae explícitamente al navegador si hace falta."""
    persisted = filter_cookies(platform, load_cookies(platform))
    if has_usable_session(platform, persisted):
        return persisted
    if browser_name:
        fallback = filter_cookies(platform, load_browser_cookies(browser_name))
        return fallback if has_usable_session(platform, fallback) else []
    return []


def clear_session(platform: str) -> bool:
    """Elimina el archivo de sesión de la plataforma. Devuelve True si existía."""
    path = session_file(platform)
    if path.exists():
        path.unlink()
        return True
    return False


def cookies_as_dict(cookies: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Convierte cookies de Playwright en un dict ``name -> value``."""
    return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}


def cookies_as_records(cookies: Sequence[Mapping[str, Any]]) -> list[Cookie]:
    """Convierte cookies de Playwright en ``Cookie`` completos (para cookiefile)."""
    records: list[Cookie] = []
    for c in cookies:
        if "name" not in c or "value" not in c:
            continue
        records.append(
            Cookie(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain", ""),
                path=c.get("path", "/"),
                secure=bool(c.get("secure", False)),
                expires=float(c.get("expires", 0) or 0),
            )
        )
    return records


def cookie_header_for_url(
    cookies: Sequence[Cookie], url: str, *, now: float | None = None
) -> str | None:
    """Devuelve las cookies cuyo dominio, ruta y vigencia permiten enviarlas a ``url``."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        _ = parsed.port  # Valida puertos no numericos o fuera de rango.
    except TypeError, ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or host is None
        or any(char.isspace() or char == "\\" for char in url)
    ):
        return None

    host = host.lower()

    def valid_host(value: str) -> bool:
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            labels = value.split(".")
            return len(value) <= 253 and all(
                label
                and len(label) <= 63
                and label[0] != "-"
                and label[-1] != "-"
                and all(char.isascii() and (char.isalnum() or char == "-") for char in label)
                for label in labels
            )

    def valid_header_cookie(cookie: Cookie) -> bool:
        name = getattr(cookie, "name", None)
        value = getattr(cookie, "value", None)
        if not isinstance(name, str) or not name or not isinstance(value, str):
            return False
        if not all(
            char.isascii() and (char.isalnum() or char in "!#$%&'*+-.^_`|~") for char in name
        ):
            return False
        return all("\x21" <= char <= "\x7e" and char not in '",;\\' for char in value)

    if not valid_host(host):
        return None

    request_path = parsed.path or "/"
    current_time = time.time() if now is None else now
    matches: list[Cookie] = []
    for cookie in cookies:
        domain_scope = getattr(cookie, "domain", None)
        path = getattr(cookie, "path", None)
        secure = getattr(cookie, "secure", None)
        expires = getattr(cookie, "expires", None)
        if (
            not valid_header_cookie(cookie)
            or not isinstance(domain_scope, str)
            or not isinstance(path, str)
            or not isinstance(secure, bool)
            or not isinstance(expires, (int, float))
        ):
            continue

        domain_scope = domain_scope.lower()
        include_subdomains = domain_scope.startswith(".")
        domain = domain_scope[1:] if include_subdomains else domain_scope
        if not valid_host(domain):
            continue

        try:
            ip_address = ipaddress.ip_address(host)
        except ValueError:
            ip_address = None
        try:
            domain_ip_address = ipaddress.ip_address(domain)
        except ValueError:
            domain_ip_address = None

        domain_matches = host == domain
        if include_subdomains and ip_address is None and domain_ip_address is None:
            domain_matches = domain_matches or host.endswith(f".{domain}")
        if not domain_matches:
            continue

        if (
            not path.startswith("/")
            or ";" in path
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in path)
        ):
            continue
        if not (
            request_path == path
            or (
                request_path.startswith(path)
                and (path.endswith("/") or request_path[len(path) :].startswith("/"))
            )
        ):
            continue
        if secure and parsed.scheme != "https":
            continue
        if not math.isfinite(expires):
            continue
        if expires > 0 and expires <= current_time:
            continue
        matches.append(cookie)

    if not matches:
        return None
    matches.sort(key=lambda cookie: -len(cookie.path))
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in matches)


@asynccontextmanager
async def browser_context(
    *, headless: bool = True, with_session: bool = True, platform: str | None = None
) -> AsyncIterator[BrowserContext]:
    """Abre un contexto de navegador con UA coherente y cookies opcionales.

    Si ``with_session`` es True, carga las cookies de ``platform`` (obligatorio
    en ese caso).

    Uso::

        async with browser_context(platform="platzi") as ctx:
            page = await ctx.new_page()
            ...
    """
    if with_session and platform is None:
        raise ValueError("browser_context requiere 'platform' cuando with_session=True")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="es-ES",
        )
        if with_session and platform is not None:
            cookies = load_cookies(platform)
            if cookies:
                await context.add_cookies(cast("Any", cookies))
        try:
            yield context
        finally:
            await context.close()
            await browser.close()


async def new_page(context: BrowserContext) -> Page:
    """Crea una página nueva en el contexto dado."""
    return await context.new_page()
