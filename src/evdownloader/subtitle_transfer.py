"""Subtitle HTTP transfer using the shared URL and credential policy."""

import contextlib
from typing import NamedTuple
from urllib.parse import urljoin

import rnet

from . import browser
from .config import RNET_IMPERSONATE
from .models import VideoSource
from .url_policy import URLPolicy, is_credential_header, parse_url_authority

MAX_SUBTITLE_BYTES = 10 * 1024 * 1024
MAX_SUBTITLE_REDIRECTS = 5


class SubtitleFetchResult(NamedTuple):
    """Transfer observations only; validation and publication belong to callers."""

    data: bytes | None
    http_status: int | None
    declared_length: str | None
    failure: str | None


def subtitle_client() -> rnet.Client:
    return rnet.Client(
        impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None),
        timeout=30,
        connect_timeout=10,
        read_timeout=30,
    )


async def fetch_subtitle(client: rnet.Client, url: str, source: VideoSource) -> SubtitleFetchResult:
    """Fetch through trusted redirects and close every response exactly once."""
    authority = parse_url_authority(url)
    suffixes = source.trusted_host_suffixes or (
        (authority.hostname,) if authority is not None else ()
    )
    policy = URLPolicy("media", suffixes)
    current_url = url
    visited: set[str] = set()
    for hop in range(MAX_SUBTITLE_REDIRECTS + 1):
        if not policy.allows(current_url):
            return SubtitleFetchResult(None, None, None, "untrusted_host")
        if current_url in visited:
            return SubtitleFetchResult(None, None, None, "redirect_loop")
        visited.add(current_url)
        headers = {
            key: value
            for key, value in source.http_headers.items()
            if not is_credential_header(key)
        }
        cookie_header = browser.cookie_header_for_url(
            source.cookie_jar,
            current_url,
            trusted_host_suffixes=source.trusted_host_suffixes,
        )
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            response = await client.get(current_url, headers=headers, allow_redirects=False)
        except Exception:  # noqa: BLE001
            return SubtitleFetchResult(None, None, None, "network_error")
        try:
            status = response.status_code
            status_code = status.as_int()
            if status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not isinstance(location, str) or not location or hop == MAX_SUBTITLE_REDIRECTS:
                    return SubtitleFetchResult(None, status_code, None, "invalid_redirect")
                current_url = urljoin(current_url, location)
                continue
            if not status.is_success():
                return SubtitleFetchResult(None, status_code, None, "http_status")
            try:
                declared_header = response.headers.get("content-length")
                if isinstance(declared_header, str):
                    try:
                        if int(declared_header) > MAX_SUBTITLE_BYTES:
                            return SubtitleFetchResult(
                                None, status_code, declared_header, "declared_too_large"
                            )
                    except ValueError:
                        pass
                return SubtitleFetchResult(
                    await response.bytes(),
                    status_code,
                    declared_header if isinstance(declared_header, str) else None,
                    None,
                )
            except Exception:  # noqa: BLE001
                return SubtitleFetchResult(None, status_code, None, "read_error")
        finally:
            with contextlib.suppress(Exception):
                await response.close()
    return SubtitleFetchResult(None, None, None, "redirect_limit")
