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
import base64
import json
import time
from unittest.mock import AsyncMock

from evdownloader.config import Settings, session_file
from evdownloader.drm.license import UDEMY_WIDEVINE_PROXY_URL
from evdownloader.drm.token_cache import DrmTokenCache, _decode_jwt_exp
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
    assert src.drm is None


def test_resolve_video_con_use_drm_detecta_metadata() -> None:
    ex = UdemyExtractor()
    ex.configure(Settings(cookies_from_browser="brave", use_drm=True))
    unit = Unit(
        title="x",
        url=(
            "https://www.udemy.com/course/learn/v4/t/lecture/53292187"
            "#__youtubedl_smuggle=%7B%22course_id%22%3A+%226905411%22%7D"
        ),
        type=UnitType.VIDEO,
        index=1,
    )

    async def fake_fetch_drm_asset(course_id: str, lecture_id: str) -> dict:
        assert course_id == "6905411"
        assert lecture_id == "53292187"
        return {
            "course_is_drmed": True,
            "media_license_token": "jwt-token",
            "media_sources": [
                {
                    "type": "application/dash+xml",
                    "src": "https://dash-enc-cdn77.udemycdn.com/cmaf/asset/cenc/stream.mpd",
                }
            ],
        }

    async def fake_fetch_text(url: str) -> str:
        assert url.endswith("stream.mpd")
        return """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2011">
  <Period><AdaptationSet mimeType="video/mp4">
    <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"
                       cenc:default_KID="fbf0dce4-2f8b-48b2-9229-1629595c0170"/>
    <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed">
      <cenc:pssh>AAAAV3Bzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAADc=</cenc:pssh>
    </ContentProtection>
  </AdaptationSet></Period>
</MPD>"""

    ex._fetch_drm_asset = fake_fetch_drm_asset  # type: ignore[method-assign]
    ex._fetch_text = fake_fetch_text  # type: ignore[method-assign]

    src = asyncio.run(ex.resolve_video(None, unit))
    assert src is not None
    # DRM mode: URL is the MPD directly (yt-dlp receives MPD, not lecture page).
    assert src.url == "https://dash-enc-cdn77.udemycdn.com/cmaf/asset/cenc/stream.mpd"
    assert src.is_embed is False
    assert src.write_subs is False
    assert src.drm is not None
    assert src.drm.scheme == "widevine"
    assert src.drm.token == "jwt-token"
    assert src.drm.key_id == "fbf0dce4-2f8b-48b2-9229-1629595c0170"


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


# -- DRM token cache ----------------------------------------------------------


def _make_jwt(exp: float) -> str:
    """Build a minimal JWT-like string with the given ``exp`` claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"fake-sig").rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


def _drm_asset(token: str | None = None) -> dict:
    return {
        "course_is_drmed": True,
        "media_license_token": token,
        "media_sources": [
            {
                "type": "application/dash+xml",
                "src": "https://dash-enc-cdn77.udemycdn.com/cmaf/asset/cenc/stream.mpd",
            }
        ],
    }


def _mpd_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2011">
  <Period><AdaptationSet mimeType="video/mp4">
    <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"
                       cenc:default_KID="fbf0dce4-2f8b-48b2-9229-1629595c0170"/>
    <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed">
      <cenc:pssh>AAAAV3Bzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAADc=</cenc:pssh>
    </ContentProtection>
  </AdaptationSet></Period>
</MPD>"""


def test_cache_reuses_valid_token() -> None:
    """First resolution fetches asset; second reuses cache (no refetch)."""
    ex = UdemyExtractor()
    ex.configure(Settings(cookies_from_browser="brave", use_drm=True))
    unit = Unit(
        title="x",
        url=(
            "https://www.udemy.com/course/learn/v4/t/lecture/53292187"
            "#__youtubedl_smuggle=%7B%22course_id%22%3A+%226905411%22%7D"
        ),
        type=UnitType.VIDEO,
        index=1,
    )
    token = _make_jwt(time.time() + 3600)
    asset = _drm_asset(token)
    fetch_mock = AsyncMock(return_value=asset)
    ex._fetch_drm_asset = fetch_mock  # type: ignore[method-assign]
    ex._fetch_text = AsyncMock(return_value=_mpd_xml())  # type: ignore[method-assign]

    src1 = asyncio.run(ex.resolve_video(None, unit))
    src2 = asyncio.run(ex.resolve_video(None, unit))

    assert src1 is not None and src1.drm is not None
    assert src2 is not None and src2.drm is not None
    assert fetch_mock.call_count == 1


def test_cache_rejects_expired_token() -> None:
    """Expired token is not reused and causes a refetch."""
    ex = UdemyExtractor()
    ex.configure(Settings(cookies_from_browser="brave", use_drm=True))
    unit = Unit(
        title="x",
        url=(
            "https://www.udemy.com/course/learn/v4/t/lecture/53292187"
            "#__youtubedl_smuggle=%7B%22course_id%22%3A+%226905411%22%7D"
        ),
        type=UnitType.VIDEO,
        index=1,
    )
    expired_token = _make_jwt(time.time() - 100)
    valid_token = _make_jwt(time.time() + 3600)
    call_count = 0

    async def fetch_drm_asset(course_id: str, lecture_id: str) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _drm_asset(expired_token)
        return _drm_asset(valid_token)

    ex._fetch_drm_asset = fetch_drm_asset  # type: ignore[method-assign]
    ex._fetch_text = AsyncMock(return_value=_mpd_xml())  # type: ignore[method-assign]

    src1 = asyncio.run(ex.resolve_video(None, unit))
    src2 = asyncio.run(ex.resolve_video(None, unit))

    assert src1 is not None and src1.drm is not None
    assert src2 is not None and src2.drm is not None
    assert call_count == 2


def test_cache_rejects_malformed_token() -> None:
    """Malformed token is not cached, causing refetch."""
    ex = UdemyExtractor()
    ex.configure(Settings(cookies_from_browser="brave", use_drm=True))
    unit = Unit(
        title="x",
        url=(
            "https://www.udemy.com/course/learn/v4/t/lecture/53292187"
            "#__youtubedl_smuggle=%7B%22course_id%22%3A+%226905411%22%7D"
        ),
        type=UnitType.VIDEO,
        index=1,
    )
    call_count = 0

    async def fetch_drm_asset(course_id: str, lecture_id: str) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _drm_asset("not-a-jwt-at-all")
        return _drm_asset(_make_jwt(time.time() + 3600))

    ex._fetch_drm_asset = fetch_drm_asset  # type: ignore[method-assign]
    ex._fetch_text = AsyncMock(return_value=_mpd_xml())  # type: ignore[method-assign]

    src1 = asyncio.run(ex.resolve_video(None, unit))
    src2 = asyncio.run(ex.resolve_video(None, unit))

    assert src1 is not None and src1.drm is not None
    assert src2 is not None and src2.drm is not None
    assert call_count == 2


def test_decode_jwt_exp_valid() -> None:
    exp = time.time() + 7200
    assert _decode_jwt_exp(_make_jwt(exp)) == exp


def test_decode_jwt_exp_malformed() -> None:
    assert _decode_jwt_exp("not-a-jwt") is None
    assert _decode_jwt_exp("a.b.c") is None  # base64 garbage


def test_cache_put_rejects_no_token() -> None:
    cache = DrmTokenCache()
    assert not cache.put("c1", "l1", {"course_is_drmed": True})
    assert cache.get("c1", "l1") is None


def test_cache_put_rejects_expired() -> None:
    cache = DrmTokenCache()
    asset = _drm_asset(_make_jwt(time.time() - 10))
    assert not cache.put("c1", "l1", asset)
    assert cache.get("c1", "l1") is None


def test_cache_get_returns_none_after_expiry() -> None:
    cache = DrmTokenCache(skew=3600)
    asset = _drm_asset(_make_jwt(time.time() + 100))
    cache.put("c1", "l1", asset)
    # skew=3600 makes expires_at = exp - 3600, which is in the past
    assert cache.get("c1", "l1") is None


# -- _attach_drm integration: default proxy URL and token override ----------


def test_attach_drm_applies_default_proxy_url() -> None:
    """When no CLI override, _attach_drm sets the Udemy Widevine proxy URL."""
    ex = UdemyExtractor()
    ex.configure(Settings(cookies_from_browser="brave", use_drm=True))
    unit = Unit(
        title="x",
        url=(
            "https://www.udemy.com/course/learn/v4/t/lecture/53292187"
            "#__youtubedl_smuggle=%7B%22course_id%22%3A+%226905411%22%7D"
        ),
        type=UnitType.VIDEO,
        index=1,
    )

    async def fake_fetch_drm_asset(course_id: str, lecture_id: str) -> dict:
        return _drm_asset(_make_jwt(time.time() + 3600))

    ex._fetch_drm_asset = fake_fetch_drm_asset  # type: ignore[method-assign]
    ex._fetch_text = AsyncMock(return_value=_mpd_xml())  # type: ignore[method-assign]

    src = asyncio.run(ex.resolve_video(None, unit))
    assert src is not None and src.drm is not None
    assert src.drm.license_url == UDEMY_WIDEVINE_PROXY_URL


def test_attach_drm_cli_license_server_overrides_default() -> None:
    """CLI --drm-license-server wins over the default proxy URL."""
    ex = UdemyExtractor()
    ex.configure(Settings(
        cookies_from_browser="brave",
        use_drm=True,
        drm_license_server="https://my-server.com/license",
    ))
    unit = Unit(
        title="x",
        url=(
            "https://www.udemy.com/course/learn/v4/t/lecture/53292187"
            "#__youtubedl_smuggle=%7B%22course_id%22%3A+%226905411%22%7D"
        ),
        type=UnitType.VIDEO,
        index=1,
    )

    async def fake_fetch_drm_asset(course_id: str, lecture_id: str) -> dict:
        return _drm_asset(_make_jwt(time.time() + 3600))

    ex._fetch_drm_asset = fake_fetch_drm_asset  # type: ignore[method-assign]
    ex._fetch_text = AsyncMock(return_value=_mpd_xml())  # type: ignore[method-assign]

    src = asyncio.run(ex.resolve_video(None, unit))
    assert src is not None and src.drm is not None
    assert src.drm.license_url == "https://my-server.com/license"


def test_attach_drm_cli_token_overrides_provider_token() -> None:
    """CLI --drm-token wins over the asset-level token."""
    ex = UdemyExtractor()
    ex.configure(Settings(
        cookies_from_browser="brave",
        use_drm=True,
        drm_token="cli-token-override",
    ))
    unit = Unit(
        title="x",
        url=(
            "https://www.udemy.com/course/learn/v4/t/lecture/53292187"
            "#__youtubedl_smuggle=%7B%22course_id%22%3A+%226905411%22%7D"
        ),
        type=UnitType.VIDEO,
        index=1,
    )

    async def fake_fetch_drm_asset(course_id: str, lecture_id: str) -> dict:
        return _drm_asset(_make_jwt(time.time() + 3600))

    ex._fetch_drm_asset = fake_fetch_drm_asset  # type: ignore[method-assign]
    ex._fetch_text = AsyncMock(return_value=_mpd_xml())  # type: ignore[method-assign]

    src = asyncio.run(ex.resolve_video(None, unit))
    assert src is not None and src.drm is not None
    assert src.drm.token == "cli-token-override"
