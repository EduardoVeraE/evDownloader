"""Utilidades: slugify de nombres, decorador de reintentos y helpers de FS."""

from __future__ import annotations

import asyncio
import functools
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from unidecode import unidecode

T = TypeVar("T")

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_SPACE = re.compile(r"\s+")


def slugify(text: str, *, max_len: int = 120) -> str:
    """Convierte un título en un nombre de archivo/carpeta seguro.

    Translitera a ASCII (vía ``unidecode``), elimina caracteres inválidos para
    el sistema de archivos y colapsa espacios. Conserva mayúsculas y guiones.
    """
    text = unidecode(text).strip()
    text = _INVALID_FS_CHARS.sub("", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = text.strip(" .")
    return text[:max_len].strip() or "untitled"


def numbered(index: int, name: str) -> str:
    """Prefija un nombre con un índice de dos dígitos: ``01-Introducción``."""
    return f"{index:02d}-{slugify(name)}"


def retry(
    attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Reintenta una corrutina ante fallos transitorios con backoff exponencial."""

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: object, **kwargs: object) -> T:
            current = delay
            last_exc: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last_exc = exc
                    if attempt == attempts:
                        break
                    await asyncio.sleep(current)
                    current *= backoff
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


def safe_mkdir(path: Path) -> Path:
    """Crea un directorio (y padres) y lo devuelve."""
    path.mkdir(parents=True, exist_ok=True)
    return path
