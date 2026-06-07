"""Extractores por plataforma.

Cada extractor sabe cómo listar la estructura de un curso y resolver la fuente
de video de cada lección. El núcleo (sesión, descarga, CLI) es agnóstico a la
plataforma para poder añadir nuevas sin reescribirlo.
"""

from __future__ import annotations

from .base import Extractor
from .platzi import PlatziExtractor

_EXTRACTORS: list[type[Extractor]] = [PlatziExtractor]


def get_extractor(url: str) -> Extractor:
    """Devuelve la instancia de extractor que soporta la URL dada."""
    for cls in _EXTRACTORS:
        if cls.supports(url):
            return cls()
    raise ValueError(f"No hay extractor que soporte la URL: {url}")


__all__ = ["Extractor", "PlatziExtractor", "get_extractor"]
