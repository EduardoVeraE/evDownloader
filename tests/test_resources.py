"""Tests del material complementario (tarea mxv): clasificación de recursos
y derivación de nombres de archivo.
"""

from __future__ import annotations

from video_downloader.extractors.platzi import PlatziExtractor
from video_downloader.models import ResourceKind
from video_downloader.service import _filename_from_url


def test_recurso_de_static_platzi_es_archivo() -> None:
    url = "https://static.platzi.com/media/uploads/pagina_html_6c81946fbc.zip?updated_at=x"
    assert PlatziExtractor._resource_kind(url) is ResourceKind.FILE


def test_recurso_externo_es_enlace() -> None:
    assert PlatziExtractor._resource_kind("https://www.google.com/chrome/") is ResourceKind.LINK


def test_filename_usa_basename_de_url() -> None:
    url = "https://static.platzi.com/media/uploads/pagina_html_6c81946fbc.zip?updated_at=x"
    assert _filename_from_url(url, "pagina.html | Recurso") == "pagina_html_6c81946fbc.zip"


def test_filename_cae_al_titulo_sin_extension() -> None:
    # URL sin nombre de archivo con extensión -> se usa el título.
    assert _filename_from_url("https://platzi.com/recurso/123", "Guía rápida") == "Guia rapida"
