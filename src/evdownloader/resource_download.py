"""Bounded, credential-safe downloads for provider-owned resources."""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin

import rnet

from . import browser
from .config import RNET_IMPERSONATE
from .models import Cookie
from .url_policy import URLPolicy

MAX_RESOURCE_BYTES = 100 * 1024 * 1024
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_REQUEST_TIMEOUT_S = 30
_CONNECT_TIMEOUT_S = 10
_READ_TIMEOUT_S = 30


@dataclass(frozen=True, slots=True)
class ResourceDownloadResult:
    ok: bool
    reason: str | None = None
    http_status: int | None = None
    error_name: str | None = None


def exception_name(exc: BaseException) -> str:
    """Return observable exception metadata without rendering sensitive values."""
    return type(exc).__name__


def _trusted_url(url: str, trusted_host_suffixes: tuple[str, ...]) -> bool:
    return URLPolicy("resource", trusted_host_suffixes).allows(url)


def _status_code(response: object) -> int:
    status = cast("Any", response).status_code
    status = status() if callable(status) else status
    as_int = getattr(status, "as_int", None)
    return as_int() if callable(as_int) else int(status)


async def download_resource(
    url: str,
    destination: Path,
    *,
    cookie_jar: list[Cookie],
    trusted_host_suffixes: tuple[str, ...],
    max_bytes: int = MAX_RESOURCE_BYTES,
) -> ResourceDownloadResult:
    """Download through validated redirects and atomically publish a regular file."""
    client = rnet.Client(
        impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None),
        timeout=_REQUEST_TIMEOUT_S,
        connect_timeout=_CONNECT_TIMEOUT_S,
        read_timeout=_READ_TIMEOUT_S,
    )
    current_url = url
    visited: set[str] = set()
    temporary_name: str | None = None
    try:
        for hop in range(MAX_REDIRECTS + 1):
            if not _trusted_url(current_url, trusted_host_suffixes):
                return ResourceDownloadResult(False, "untrusted_host")
            if current_url in visited:
                return ResourceDownloadResult(False, "redirect_loop")
            visited.add(current_url)

            headers: dict[str, str] = {}
            cookie_header = browser.cookie_header_for_url(
                cookie_jar,
                current_url,
                trusted_host_suffixes=trusted_host_suffixes,
            )
            if cookie_header:
                headers["Cookie"] = cookie_header
            try:
                response = await client.get(current_url, headers=headers, allow_redirects=False)
            except Exception as exc:  # noqa: BLE001 - exception values may contain signed URLs
                return ResourceDownloadResult(
                    False, "network_error", error_name=exception_name(exc)
                )

            try:
                try:
                    status = _status_code(response)
                except Exception as exc:  # noqa: BLE001 - status values are untrusted
                    return ResourceDownloadResult(
                        False, "invalid_status", error_name=exception_name(exc)
                    )
                if status in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not isinstance(location, str) or not location:
                        return ResourceDownloadResult(False, "invalid_redirect", status)
                    if hop == MAX_REDIRECTS:
                        return ResourceDownloadResult(False, "redirect_limit", status)
                    current_url = urljoin(current_url, location)
                    continue
                if not 200 <= status < 300:
                    return ResourceDownloadResult(False, "http_status", status)

                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > max_bytes:
                            return ResourceDownloadResult(False, "declared_too_large", status)
                    except TypeError, ValueError:
                        pass

                size = 0
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=destination.parent,
                        prefix=f".{destination.name}.",
                        suffix=".tmp",
                        delete=False,
                    ) as staging:
                        temporary_name = staging.name
                        async for chunk in response.stream():
                            size += len(chunk)
                            if size > max_bytes:
                                return ResourceDownloadResult(False, "body_too_large", status)
                            staging.write(chunk)
                    if size == 0:
                        return ResourceDownloadResult(False, "empty", status)
                    os.replace(temporary_name, destination)
                    temporary_name = None
                except Exception as exc:  # noqa: BLE001 - exception values may contain secrets
                    return ResourceDownloadResult(
                        False, "write_or_read_error", status, exception_name(exc)
                    )
                return ResourceDownloadResult(True)
            finally:
                with contextlib.suppress(Exception):
                    await response.close()
    finally:
        if temporary_name is not None:
            with contextlib.suppress(OSError):
                Path(temporary_name).unlink()

    return ResourceDownloadResult(False, "redirect_limit")
