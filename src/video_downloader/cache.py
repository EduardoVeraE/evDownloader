"""Caché simple en disco para la estructura de cursos (JSON por URL)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .config import CACHE_DIR, ensure_dirs


def _key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def get(url: str) -> dict[str, Any] | None:
    """Devuelve la estructura cacheada para una URL, o ``None``."""
    path = CACHE_DIR / f"{_key(url)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def set(url: str, data: dict[str, Any]) -> None:
    """Guarda la estructura de un curso en caché."""
    ensure_dirs()
    path = CACHE_DIR / f"{_key(url)}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clear() -> int:
    """Borra toda la caché. Devuelve el número de archivos eliminados."""
    if not CACHE_DIR.exists():
        return 0
    count = 0
    for f in CACHE_DIR.glob("*.json"):
        f.unlink()
        count += 1
    return count
