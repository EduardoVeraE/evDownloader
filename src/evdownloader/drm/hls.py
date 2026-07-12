"""HLS m3u8 manifest parser for DRM detection."""

from __future__ import annotations

import re

from ..models import DrmInfo

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=((?:"[^"]*")|[^,]*)', re.IGNORECASE)

# Known KEYFORMAT values
_KEYFORMAT_FAIRPLAY = "com.apple.streamingkeydelivery"


def _normalize_method(method: str) -> str:
    """Normalize a method string to lowercase with hyphens."""
    return method.lower().replace("_", "-")


def _parse_attributes(value: str) -> dict[str, str]:
    """Parse an HLS attribute list, preserving quoted commas."""
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(value):
        key = match.group(1).upper()
        raw = match.group(2).strip()
        attrs[key] = raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
    return attrs


def parse_hls(manifest_text: str) -> list[DrmInfo]:
    """Parse an HLS m3u8 manifest and extract DRM/encryption information.

    Scans for EXT-X-KEY directives and classifies them by METHOD and
    KEYFORMAT. Only returns entries for recognized schemes (AES-128,
    FairPlay, or Sample-AES with Apple delivery).

    Args:
        manifest_text: The raw m3u8 playlist content.

    Returns:
        List of DrmInfo objects for each distinct DRM/encryption scheme found.
    """
    seen_schemes: set[str] = set()
    results: list[DrmInfo] = []

    for line in manifest_text.splitlines():
        if not line.upper().startswith("#EXT-X-KEY:"):
            continue
        attrs = _parse_attributes(line.split(":", 1)[1])
        method = attrs.get("METHOD")
        if not method:
            continue
        uri = attrs.get("URI")
        keyformat = attrs.get("KEYFORMAT")

        method_upper = method.upper().strip()

        # Classify the scheme
        if method_upper == "NONE":
            continue
        elif method_upper == "AES-128":
            scheme = "aes-128"
        elif keyformat and keyformat.lower() == _KEYFORMAT_FAIRPLAY:
            scheme = "fairplay"
        elif keyformat:
            # Preserve unknown KEYFORMAT as a custom scheme label
            scheme = keyformat.lower()
        elif method_upper == "SAMPLE-AES":
            scheme = "sample-aes"
        else:
            scheme = _normalize_method(method_upper)

        if scheme in seen_schemes:
            continue
        seen_schemes.add(scheme)

        # Build DrmInfo
        info = DrmInfo(scheme=scheme)
        if uri:
            # For FairPlay, the URI is the key server URL
            info.license_url = uri
        results.append(info)

    return results
