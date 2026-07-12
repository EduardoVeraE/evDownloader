"""Top-level DRM detector — dispatches to MPD or HLS parser."""

from __future__ import annotations

from urllib.parse import urlsplit

from .hls import parse_hls
from .models import DrmDetectionResult
from .mpd import parse_mpd


def detect_drm(
    manifest_text: str,
    url: str | None = None,
    content_type: str | None = None,
) -> DrmDetectionResult:
    """Detect DRM systems from a manifest.

    Accepts raw manifest text plus optional URL or content-type hints
    to determine whether to parse as DASH MPD or HLS m3u8.

    Args:
        manifest_text: Raw manifest content (XML for MPD, text for m3u8).
        url: Optional URL of the manifest (used to infer type).
        content_type: Optional Content-Type header value.

    Returns:
        DrmDetectionResult with the list of DRM systems found.
    """
    manifest_type = _infer_manifest_type(manifest_text, url, content_type)

    if manifest_type == "mpd":
        systems = parse_mpd(manifest_text)
    elif manifest_type == "m3u8":
        systems = parse_hls(manifest_text)
    else:
        systems = []

    return DrmDetectionResult(
        systems=systems,
        manifest_type=manifest_type,
    )


def _infer_manifest_type(
    manifest_text: str,
    url: str | None = None,
    content_type: str | None = None,
) -> str:
    """Infer manifest type from content, URL, or content-type.

    Returns "mpd", "m3u8", or "unknown".
    """
    # Check content-type header first
    if content_type:
        ct = content_type.lower()
        if "dash" in ct or "mpd" in ct:
            return "mpd"
        if "mpegurl" in ct or "m3u8" in ct:
            return "m3u8"

    # Check URL extension
    if url:
        path = urlsplit(url).path.lower()
        if path.endswith(".mpd"):
            return "mpd"
        if path.endswith(".m3u8"):
            return "m3u8"

    # Check content sniffing
    text = manifest_text.strip()
    if text.startswith("<?xml") or text.startswith("<MPD") or "<MPD" in text[:500]:
        return "mpd"
    if text.startswith("#EXTM3U") or "#EXT-X-" in text[:500]:
        return "m3u8"

    return "unknown"
