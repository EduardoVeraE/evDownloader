"""Extractores por plataforma.

Cada extractor sabe cómo listar la estructura de un curso y resolver la fuente
de video de cada lección. El núcleo (sesión, descarga, CLI) es agnóstico a la
plataforma para poder añadir nuevas sin reescribirlo.
"""

from __future__ import annotations

from .base import Extractor
from .codigofacilito import CodigofacilitoExtractor
from .platzi import PlatziExtractor
from .udemy import UdemyExtractor

_EXTRACTORS: list[type[Extractor]] = [
    PlatziExtractor,
    UdemyExtractor,
    CodigofacilitoExtractor,
]
_BY_NAME: dict[str, type[Extractor]] = {cls.name: cls for cls in _EXTRACTORS}


def get_extractor(url: str) -> Extractor:
    """Devuelve la instancia de extractor que soporta la URL dada."""
    for cls in _EXTRACTORS:
        if cls.supports(url):
            return cls()
    raise ValueError(f"No hay extractor que soporte la URL: {url}")


def get_extractor_by_name(name: str) -> Extractor:
    """Devuelve la instancia de extractor registrada con ese nombre de plataforma."""
    cls = _BY_NAME.get(name)
    if cls is None:
        disponibles = ", ".join(sorted(_BY_NAME))
        raise ValueError(f"Plataforma desconocida: {name!r}. Disponibles: {disponibles}")
    return cls()


__all__ = [
    "CodigofacilitoExtractor",
    "Extractor",
    "PlatziExtractor",
    "UdemyExtractor",
    "get_extractor",
    "get_extractor_by_name",
]
