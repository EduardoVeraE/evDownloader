"""Tests de las funciones puras del extractor de Udemy (enumeración por API 2.0).

Cubren, sin red ni yt-dlp real:
* ``supports`` / registro / ``needs_browser``.
* ``configure`` propaga el navegador de cookies.
* ``_build_course`` agrupa el currículum de la API 2.0 en capítulos y emite las
  URLs "smuggleadas" con el course_id.
* ``list_course`` exige ``--cookies-from-browser``.
* ``resolve_video`` devuelve la URL de la lección para que la resuelva yt-dlp.
* Separación de sesión por plataforma (``config.session_file``).
"""

from __future__ import annotations

import asyncio

from evdownloader.config import Settings, session_file
from evdownloader.extractors import get_extractor, get_extractor_by_name
from evdownloader.extractors.udemy import UdemyExtractor
from evdownloader.models import ResourceKind, Unit, UnitType


# Fixtures con el formato de cached-subscriber-curriculum-items de la API 2.0.
def _chapter(title: str, index: int) -> dict:
    return {"_class": "chapter", "title": title, "object_index": index}


def _lecture(id_: str, title: str, asset_type: str = "Video") -> dict:
    return {"_class": "lecture", "id": id_, "title": title, "asset": {"asset_type": asset_type}}


_CURRICULUM = [
    _chapter("Sobre el curso", 1),
    _lecture("1", "Intro"),
    _lecture("2", "Bienvenida"),
    _chapter("Introducción", 2),
    _lecture("3", "Crear cuenta"),
]


# -- Enrutado / capacidades ---------------------------------------------------
def test_supports_reconoce_udemy() -> None:
    assert UdemyExtractor.supports("https://www.udemy.com/course/foo/")
    assert not UdemyExtractor.supports("https://platzi.com/cursos/foo/")


def test_get_extractor_por_url() -> None:
    assert isinstance(get_extractor("https://www.udemy.com/course/x/"), UdemyExtractor)


def test_get_extractor_por_nombre() -> None:
    assert isinstance(get_extractor_by_name("udemy"), UdemyExtractor)


def test_no_necesita_navegador() -> None:
    # Clave del rediseño: Udemy NO abre Playwright (evita Cloudflare).
    assert UdemyExtractor.needs_browser is False


def test_configure_propaga_navegador_de_cookies() -> None:
    ex = UdemyExtractor()
    ex.configure(Settings(cookies_from_browser="brave"))
    assert ex._cookies_from_browser == "brave"


# -- Construcción del curso desde el currículum de la API 2.0 ----------------
def test_build_course_agrupa_por_capitulo_e_indexa() -> None:
    course = UdemyExtractor()._build_course(
        "https://www.udemy.com/course/x/", "42", _CURRICULUM, title_override="Curso de Azure"
    )

    assert course.title == "Curso de Azure"
    assert [c.title for c in course.chapters] == ["Sobre el curso", "Introducción"]
    assert [c.index for c in course.chapters] == [1, 2]
    # Índices globales de unidad, consecutivos entre capítulos.
    assert [u.index for ch in course.chapters for u in ch.units] == [1, 2, 3]
    assert course.chapters[1].units[0].title == "Crear cuenta"
    # Todas las unidades son video (las no-video se omiten).
    assert all(u.type is UnitType.VIDEO for ch in course.chapters for u in ch.units)


def test_build_course_emite_url_smuggleada_con_course_id() -> None:
    course = UdemyExtractor()._build_course(
        "https://www.udemy.com/course/x/", "42", _CURRICULUM
    )
    url = course.chapters[0].units[0].url
    # yt-dlp lee el course_id del smuggle y no scrapea el HTML del curso.
    assert "/course/learn/v4/t/lecture/1" in url
    assert UdemyExtractor._ids_from_url(url) == ("42", "1")


def test_build_course_titulo_por_defecto() -> None:
    # Sin override (título de la API vacío) cae a "Curso".
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", "42", _CURRICULUM)
    assert course.title == "Curso"


def test_build_course_omite_lecciones_no_video() -> None:
    items = [
        _chapter("S", 1),
        _lecture("1", "Video", "Video"),
        _lecture("2", "Artículo", "Article"),  # se omite
    ]
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", "42", items)
    assert sum(len(c.units) for c in course.chapters) == 1
    assert course.chapters[0].units[0].title == "Video"


def test_build_course_sin_items() -> None:
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", "42", [])
    assert course.chapters == []


def test_build_course_leccion_suelta_sin_capitulo() -> None:
    # Lección antes de cualquier capítulo -> se crea "Sección 1".
    items = [_lecture("1", "Suelta")]
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", "42", items)
    assert course.chapters[0].title == "Sección 1"
    assert len(course.chapters[0].units) == 1


# -- list_course exige cookies del navegador ---------------------------------
def test_list_course_sin_cookies_lanza() -> None:
    ex = UdemyExtractor()  # sin configure -> sin cookies_from_browser
    try:
        asyncio.run(ex.list_course(None, "https://www.udemy.com/course/x/"))
    except ValueError as e:
        assert "cookies-from-browser" in str(e)
    else:
        raise AssertionError("Se esperaba ValueError por falta de --cookies-from-browser")


# -- resolve_video no navega: entrega la URL de la lección -------------------
def test_resolve_video_devuelve_url_de_leccion() -> None:
    ex = UdemyExtractor()
    unit = Unit(title="x", url="https://www.udemy.com/x/lecture/1", type=UnitType.VIDEO, index=1)
    src = asyncio.run(ex.resolve_video(None, unit))
    assert src is not None
    assert src.url == "https://www.udemy.com/x/lecture/1"
    assert src.is_embed is True
    # yt-dlp debe extraer los subtítulos junto con el video.
    assert src.write_subs is True


def test_resolve_video_ignora_no_video() -> None:
    ex = UdemyExtractor()
    unit = Unit(title="q", url="https://www.udemy.com/x/quiz/1", type=UnitType.QUIZ, index=1)
    assert asyncio.run(ex.resolve_video(None, unit)) is None


# -- Recursos suplementarios (adjuntos y enlaces) ----------------------------
def test_ids_from_url_extrae_course_y_lecture() -> None:
    url = (
        "https://www.udemy.com/course-dashboard-redirect/learn/v4/t/lecture/49299317"
        "#__youtubedl_smuggle=%7B%22course_id%22%3A+%223984982%22%7D"
    )
    assert UdemyExtractor._ids_from_url(url) == ("3984982", "49299317")


def test_ids_from_url_sin_datos() -> None:
    assert UdemyExtractor._ids_from_url("https://www.udemy.com/course/x/") == (None, None)


def test_assets_to_resources_archivo_usa_filename() -> None:
    assets = [
        {
            "asset_type": "File",
            "title": "RECURSOS WEB.pdf",
            "filename": "RECURSOS-WEB.pdf",
            "external_url": "",
            "download_urls": {"File": [{"label": "download", "file": "https://att-c.udemycdn.com/x/original.pdf?Signature=abc"}]},
        }
    ]
    res = UdemyExtractor._assets_to_resources(assets)
    assert len(res) == 1
    assert res[0].kind is ResourceKind.FILE
    # Usa el filename real (no "original.pdf" de la URL) para evitar colisiones.
    assert res[0].title == "RECURSOS-WEB.pdf"
    assert res[0].url.startswith("https://att-c.udemycdn.com/")


def test_assets_to_resources_enlace_externo() -> None:
    assets = [{"asset_type": "ExternalLink", "title": "Repo", "external_url": "https://github.com/x"}]
    res = UdemyExtractor._assets_to_resources(assets)
    assert len(res) == 1
    assert res[0].kind is ResourceKind.LINK
    assert res[0].url == "https://github.com/x"


def test_assets_to_resources_omite_sin_url() -> None:
    assets = [{"asset_type": "File", "title": "x", "external_url": "", "download_urls": {}}]
    assert UdemyExtractor._assets_to_resources(assets) == []


# -- Sesión por plataforma ----------------------------------------------------
def test_session_file_separa_por_plataforma() -> None:
    assert session_file("platzi").name == "session-platzi.json"
    assert session_file("udemy").name == "session-udemy.json"
    assert session_file("platzi") != session_file("udemy")
