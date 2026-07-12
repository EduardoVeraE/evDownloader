"""In-memory cache for Udemy DRM license tokens (JWT-based expiry).

Avoids redundant Udemy API calls and Cloudflare friction by reusing the
``media_license_token`` until its JWT ``exp`` claim minus a safety skew.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from typing import Any

_DEFAULT_SKEW_S = 60


def _decode_jwt_exp(token: str) -> float | None:
    """Return the ``exp`` claim from a JWT without signature verification.

    Returns ``None`` if the token is malformed or ``exp`` is missing/not numeric.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    # Restore base64url padding.
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded)
        claims = json.loads(decoded)
    except (binascii.Error, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return float(exp)
    return None


class DrmTokenCache:
    """In-memory cache keyed by ``course_id:lecture_id``.

    Entries are evicted automatically when the JWT ``exp`` minus *skew* is
    reached.
    """

    def __init__(self, *, skew: float = _DEFAULT_SKEW_S) -> None:
        self._skew = skew
        self._store: dict[str, tuple[dict[str, Any], float]] = {}

    def get(self, course_id: str, lecture_id: str) -> dict[str, Any] | None:
        """Return the cached asset dict, or ``None`` if missing/expired."""
        key = f"{course_id}:{lecture_id}"
        entry = self._store.get(key)
        if entry is None:
            return None
        asset, expires_at = entry
        if time.time() >= expires_at:
            del self._store[key]
            return None
        return asset

    def put(self, course_id: str, lecture_id: str, asset: dict[str, Any]) -> bool:
        """Cache *asset* if it contains a valid, non-expired JWT token.

        Returns ``True`` if the asset was cached, ``False`` otherwise.
        """
        token = asset.get("media_license_token")
        if not isinstance(token, str) or not token:
            return False
        exp = _decode_jwt_exp(token)
        if exp is None:
            return False
        expires_at = exp - self._skew
        if time.time() >= expires_at:
            return False
        key = f"{course_id}:{lecture_id}"
        self._store[key] = (asset, expires_at)
        return True

    def clear(self) -> None:
        """Drop all cached entries."""
        self._store.clear()
