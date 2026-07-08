"""Tests de las funciones puras del extractor de Codigofacilito.

Cubren, sin red ni yt-dlp real:
* ``supports`` / registro / ``needs_browser``.
* ``configure`` propaga el navegador de cookies.
* ``list_course`` exige ``--cookies-from-browser``.
* ``_parse_course`` extrae título, agrupa clases por módulo (en orden), decodifica
  entidades HTML y construye URLs absolutas a partir del HTML SSR del temario.
* ``_classify_icon`` mapea el icono de Material Icons al tipo de unidad.
* ``resolve_video`` delega la resolución en yt-dlp (embed de BunnyCDN).
* Separación de sesión por plataforma (``config.session_file``).
"""

from __future__ import annotations

import asyncio

from video_downloader.config import Settings, session_file
from video_downloader.extractors import get_extractor, get_extractor_by_name
from video_downloader.extractors.codigofacilito import CodigofacilitoExtractor
from video_downloader.models import Unit, UnitType


def _item(href: str, li_mods: str, icon: str, kind: str, title: str) -> str:
    """Reproduce el marcado real de una clase del temario (SSR).

    ``li_mods`` simula los modificadores de progreso que la vista autenticada
    añade al ``<li>`` (p. ej. ``topic-item--completed``) y ``icon`` el icono de
    progreso correspondiente (``done_all`` en una clase ya vista).
    """
    return (
        f'<a href="{href}"><li class=\'topic-item{li_mods}\' id=\'item-\'>\n'
        "<div class='row' style='column-gap:9px;'>\n"
        f"<i class='topic-item-icon material-icons'>\n{icon}\n</i>\n"
        '<div class="col-xs no-padding"><div class="box">'
        "<p class='no-margin h5 bold topic-item-kind'>\n"
        f"{kind}\n</p>\n"
        f"<p class='ibm f-text-16 bold topic-item-title'>{title}</p>\n"
        "</div></div></div>\n</li>\n</a>"
    )


def _module(num: int, title: str, items: str) -> str:
    """Reproduce la cabecera de módulo + su lista de clases."""
    return (
        "<header class='topics-header'>\n"
        "<span class='f-green-text f-green-text--2 bold h5'>\n"
        f"Módulo\n{num}\n</span>\n"
        "<span style='color:#C4C4C4'>|</span>\n"
        f"<h4 class='ibm bold'>{title}</h4>\n"
        "</header>\n"
        f"<div class='collapsible-body no-border topics-li block-{num}00'>\n<ul>\n"
        f"{items}\n</ul>\n</div>"
    )


# Fixture fiel a la estructura real de codigofacilito.com/cursos/{slug}.
_HTML = (
    "<html><body>\n"
    "<h1 class='h1 f-text-48 no-margin'>Curso profesional de Git</h1>\n"
    + _module(
        1,
        "Introducción",
        # Clase ya vista: modificadores de progreso + icono done_all (no play_*).
        _item(
            "/videos/intro",
            " topic-item--active topic-item--completed",
            "done_all",
            "Clase 1",
            "Introducción al Curso",
        )
        + "\n"
        + _item(
            "/videos/vcs", "", "play_circle_outline", "Clase 2", "Qué es Control de Versiones"
        ),
    )
    + "\n"
    + _module(
        2,
        "Conceptos Iniciales",
        _item(
            "/videos/git-github",
            " topic-item--completed",
            "done_all",
            "Clase 3",
            "Git &amp; Github",
        )
        + "\n"
        + _item("/videos/ramas", "", "play_circle_outline", "Clase 4", "Ramas"),
    )
    + "\n</body></html>"
)


# -- Enrutado / capacidades ---------------------------------------------------
def test_supports_reconoce_codigofacilito() -> None:
    assert CodigofacilitoExtractor.supports("https://codigofacilito.com/cursos/git-profesional")
    assert not CodigofacilitoExtractor.supports("https://platzi.com/cursos/foo/")


def test_get_extractor_por_url() -> None:
    ext = get_extractor("https://codigofacilito.com/cursos/git-profesional")
    assert isinstance(ext, CodigofacilitoExtractor)


def test_get_extractor_por_nombre() -> None:
    assert isinstance(get_extractor_by_name("codigofacilito"), CodigofacilitoExtractor)


def test_no_necesita_navegador() -> None:
    # Patrón B (como Udemy): el video lo resuelve yt-dlp, no Playwright.
    assert CodigofacilitoExtractor.needs_browser is False


# -- configure ----------------------------------------------------------------
def test_configure_propaga_cookies_from_browser() -> None:
    ext = CodigofacilitoExtractor()
    ext.configure(Settings(cookies_from_browser="brave"))
    assert ext._cookies_from_browser == "brave"


# -- list_course exige cookies ------------------------------------------------
def test_list_course_sin_cookies_falla() -> None:
    ext = CodigofacilitoExtractor()
    ext.configure(Settings())  # sin cookies_from_browser
    try:
        asyncio.run(ext.list_course(None, "https://codigofacilito.com/cursos/x"))
    except ValueError as exc:
        assert "cookies-from-browser" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("list_course debería exigir --cookies-from-browser")


# -- _parse_course ------------------------------------------------------------
def test_parse_course_titulo_y_estructura() -> None:
    course = CodigofacilitoExtractor._parse_course(
        "https://codigofacilito.com/cursos/git-profesional", _HTML
    )
    assert course.title == "Curso profesional de Git"
    assert len(course.chapters) == 2
    assert [c.title for c in course.chapters] == ["Introducción", "Conceptos Iniciales"]
    assert [c.index for c in course.chapters] == [1, 2]


def test_parse_course_agrupa_clases_por_modulo() -> None:
    course = CodigofacilitoExtractor._parse_course("https://codigofacilito.com/cursos/x", _HTML)
    m1, m2 = course.chapters
    assert [u.title for u in m1.units] == ["Introducción al Curso", "Qué es Control de Versiones"]
    # Índice global de unidad, continuo entre módulos.
    assert [u.index for u in m1.units] == [1, 2]
    assert [u.index for u in m2.units] == [3, 4]


def test_parse_course_urls_absolutas() -> None:
    course = CodigofacilitoExtractor._parse_course("https://codigofacilito.com/cursos/x", _HTML)
    assert course.chapters[0].units[0].url == "https://codigofacilito.com/videos/intro"


def test_parse_course_decodifica_entidades_html() -> None:
    course = CodigofacilitoExtractor._parse_course("https://codigofacilito.com/cursos/x", _HTML)
    git_github = course.chapters[1].units[0]
    assert git_github.title == "Git & Github"


def test_parse_course_todas_video_pese_al_icono_de_progreso() -> None:
    # El icono refleja el progreso del usuario (done_all en vistas), NO el tipo:
    # toda entrada /videos/ debe clasificarse como VIDEO igualmente.
    course = CodigofacilitoExtractor._parse_course("https://codigofacilito.com/cursos/x", _HTML)
    tipos = {u.type for c in course.chapters for u in c.units}
    assert tipos == {UnitType.VIDEO}


def test_parse_course_tolera_modificadores_de_progreso() -> None:
    # El <li> autenticado añade topic-item--completed / --active; deben parsearse.
    course = CodigofacilitoExtractor._parse_course("https://codigofacilito.com/cursos/x", _HTML)
    total = sum(len(c.units) for c in course.chapters)
    assert total == 4


def test_parse_course_html_vacio() -> None:
    course = CodigofacilitoExtractor._parse_course(
        "https://codigofacilito.com/cursos/x", "<html></html>"
    )
    assert course.title == "Curso"
    assert course.chapters == []


# -- resolve_video ------------------------------------------------------------
def test_resolve_video_delega_en_ytdlp() -> None:
    ext = CodigofacilitoExtractor()
    course = CodigofacilitoExtractor._parse_course("https://codigofacilito.com/cursos/x", _HTML)
    video_unit = course.chapters[0].units[0]
    source = asyncio.run(ext.resolve_video(None, video_unit))
    assert source is not None
    assert source.url == video_unit.url
    assert source.is_embed is True
    assert source.write_subs is True


def test_resolve_video_ignora_no_video() -> None:
    ext = CodigofacilitoExtractor()
    lectura = Unit(
        title="Lectura", url="https://codigofacilito.com/videos/x", type=UnitType.LECTURE
    )
    assert asyncio.run(ext.resolve_video(None, lectura)) is None


# -- Separación de sesión por plataforma --------------------------------------
def test_session_file_separado() -> None:
    assert session_file("codigofacilito").name == "session-codigofacilito.json"
    assert session_file("codigofacilito") != session_file("udemy")
