"""Tests de las funciones puras del extractor de Udemy (enumeración por API 2.0).

Cubren, sin red ni yt-dlp real:
* ``supports`` / registro / ``needs_browser``.
* ``configure`` propaga el navegador de fallback.
* ``_build_course`` conserva videos y artículos con sus ordinales reales y emite
  URLs "smuggleadas" con el course_id.
* ``list_course`` exige una sesión persistida o ``--cookies-from-browser``.
* ``resolve_video`` devuelve la URL de la lección para que la resuelva yt-dlp.
* Separación de sesión por plataforma (``config.session_file``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evdownloader import browser
from evdownloader.config import Settings, session_file
from evdownloader.drm.license import UDEMY_WIDEVINE_PROXY_URL
from evdownloader.drm.token_cache import DrmTokenCache, _decode_jwt_exp
from evdownloader.extractors import get_extractor, get_extractor_by_name
from evdownloader.extractors.udemy import _MAX_CURRICULUM_PAGES, UdemyExtractor
from evdownloader.models import DrmInfo, ResourceKind, Unit, UnitType


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


@pytest.fixture(autouse=True)
def no_real_udemy_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla todos los tests del archivo real de sesión del usuario."""
    monkeypatch.setattr(
        browser,
        "resolve_cookies",
        lambda platform, browser_name=None: [
            {"name": "access_token", "value": "test-session", "domain": ".udemy.com"}
        ],
    )


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
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", "42", _CURRICULUM)
    url = course.chapters[0].units[0].url
    # yt-dlp lee el course_id del smuggle y no scrapea el HTML del curso.
    assert "/course/learn/v4/t/lecture/1" in url
    assert UdemyExtractor._ids_from_url(url) == ("42", "1")


def test_build_course_titulo_por_defecto() -> None:
    # Sin override (título de la API vacío) cae a "Curso".
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", "42", _CURRICULUM)
    assert course.title == "Curso"


def test_build_course_conserva_video_articulo_video_con_ordinal_real() -> None:
    items = [
        _chapter("S", 1),
        _lecture("1", "Video inicial", "Video"),
        _lecture("2", "Artículo", "Article"),
        _lecture("3", "Video final", "Video"),
    ]
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", "42", items)
    units = course.chapters[0].units

    assert [unit.type for unit in units] == [
        UnitType.VIDEO,
        UnitType.LECTURE,
        UnitType.VIDEO,
    ]
    assert [unit.index for unit in units] == [1, 2, 3]


def test_build_course_clasifica_y_omite_asset_no_soportado_sin_reindexar() -> None:
    items = [
        _chapter("S", 1),
        _lecture("1", "Video inicial", "Video"),
        _lecture("2", "Ejercicio", "Practice"),
        _lecture("3", "Video final", "Video"),
    ]
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", "42", items)

    assert [unit.title for unit in course.chapters[0].units] == ["Video inicial", "Video final"]
    assert [unit.index for unit in course.chapters[0].units] == [1, 3]


def test_build_course_sin_items() -> None:
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", "42", [])
    assert course.chapters == []


def test_build_course_leccion_suelta_sin_capitulo() -> None:
    # Lección antes de cualquier capítulo -> se crea "Sección 1".
    items = [_lecture("1", "Suelta")]
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", "42", items)
    assert course.chapters[0].title == "Sección 1"
    assert len(course.chapters[0].units) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    ["not-json", "[]", "{}", '{"results": {}}', '{"results": [], "next": 1}'],
)
async def test_fetch_curriculum_rejects_invalid_page_payload(body: str) -> None:
    response = AsyncMock()
    response.text.return_value = body
    extractor = UdemyExtractor()
    client = AsyncMock()
    client.get.return_value = response
    extractor._client = client

    with pytest.raises(ValueError, match="currículum"):
        await extractor._fetch_curriculum("42")

    response.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_fetch_curriculum_rejects_request_failure() -> None:
    extractor = UdemyExtractor()
    client = AsyncMock()
    client.get.side_effect = RuntimeError("secret request details")
    extractor._client = client

    with pytest.raises(ValueError, match="Inténtalo de nuevo") as error:
        await extractor._fetch_curriculum("42")

    assert "secret request details" not in str(error.value)


@pytest.mark.asyncio
async def test_fetch_curriculum_discards_results_when_later_page_fails() -> None:
    first = AsyncMock()
    first.text.return_value = json.dumps(
        {"results": [_lecture("1", "Intro")], "next": "https://example.test/page/2"}
    )
    extractor = UdemyExtractor()
    client = AsyncMock()
    client.get.side_effect = [first, RuntimeError("second page failed")]
    extractor._client = client

    with pytest.raises(ValueError, match="fuera del proveedor"):
        await extractor._fetch_curriculum("42")

    assert client.get.await_count == 1
    first.close.assert_awaited_once_with()


def _curriculum_page(next_url: str | None) -> AsyncMock:
    response = AsyncMock()
    response.text.return_value = json.dumps({"results": [], "next": next_url})
    return response


@pytest.mark.asyncio
async def test_fetch_curriculum_allows_the_full_safe_page_budget() -> None:
    responses = [
        _curriculum_page(
            None
            if page == _MAX_CURRICULUM_PAGES - 1
            else f"https://www.udemy.com/api-2.0/page/{page + 1}"
        )
        for page in range(_MAX_CURRICULUM_PAGES)
    ]
    extractor = UdemyExtractor()
    client = AsyncMock()
    client.get.side_effect = responses
    extractor._client = client

    assert await extractor._fetch_curriculum("42") == []
    assert client.get.await_count == _MAX_CURRICULUM_PAGES
    assert all(response.close.await_count == 1 for response in responses)


@pytest.mark.asyncio
async def test_fetch_curriculum_rejects_page_beyond_safe_budget() -> None:
    secret = "synthetic-secret"
    responses = [
        _curriculum_page(f"https://www.udemy.com/api-2.0/page/{page + 1}?token={secret}")
        for page in range(_MAX_CURRICULUM_PAGES)
    ]
    extractor = UdemyExtractor()
    client = AsyncMock()
    client.get.side_effect = responses
    extractor._client = client

    with pytest.raises(ValueError, match="límite seguro") as error:
        await extractor._fetch_curriculum("42")

    assert secret not in str(error.value)
    assert "https://" not in str(error.value)
    assert client.get.await_count == _MAX_CURRICULUM_PAGES
    assert all(response.close.await_count == 1 for response in responses)


# -- list_course exige una fuente de credenciales -----------------------------
def test_list_course_sin_cookies_lanza() -> None:
    ex = UdemyExtractor()  # sin sesión persistida ni cookies_from_browser
    with patch("evdownloader.extractors.udemy.browser.resolve_cookies", return_value=[]):
        try:
            asyncio.run(ex.list_course(None, "https://www.udemy.com/course/x/"))
        except ValueError as e:
            message = str(e)
            assert "evd login udemy" in message
            assert "cookies-from-browser" in message
        else:
            raise AssertionError("Se esperaba ValueError por falta de credenciales")


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


def test_resolve_video_comparte_sesion_persistida_con_ytdlp() -> None:
    cookies = [
        {"name": "access_token", "value": "not-for-output", "domain": ".udemy.com"},
        {"name": "sid", "value": "third-party", "domain": ".google.com"},
    ]
    ex = UdemyExtractor()
    ex.configure(Settings(cookies_from_browser="brave"))

    with patch("evdownloader.extractors.udemy.browser.resolve_cookies", return_value=cookies):
        unit = Unit(title="x", url="https://www.udemy.com/x/lecture/1", index=1)
        src = asyncio.run(ex.resolve_video(None, unit))

    assert src is not None
    assert src.cookies == {}
    assert src.cookie_jar[0].name == "access_token"
    assert len(src.cookie_jar) == 1
    assert src.trusted_host_suffixes == ("udemy.com", "udemycdn.com")


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
            "download_urls": {
                "File": [
                    {
                        "label": "download",
                        "file": "https://att-c.udemycdn.com/x/original.pdf?Signature=abc",
                    }
                ]
            },
        }
    ]
    res = UdemyExtractor._assets_to_resources(assets)
    assert len(res) == 1
    assert res[0].kind is ResourceKind.FILE
    # Usa el filename real (no "original.pdf" de la URL) para evitar colisiones.
    assert res[0].title == "RECURSOS-WEB.pdf"
    assert res[0].url.startswith("https://att-c.udemycdn.com/")


def test_assets_to_resources_enlace_externo() -> None:
    assets = [
        {"asset_type": "ExternalLink", "title": "Repo", "external_url": "https://github.com/x"}
    ]
    res = UdemyExtractor._assets_to_resources(assets)
    assert len(res) == 1
    assert res[0].kind is ResourceKind.LINK
    assert res[0].url == "https://github.com/x"


def test_assets_to_resources_omite_sin_url() -> None:
    assets = [{"asset_type": "File", "title": "x", "external_url": "", "download_urls": {}}]
    assert UdemyExtractor._assets_to_resources(assets) == []


def _article_unit() -> Unit:
    course = UdemyExtractor()._build_course(
        "https://www.udemy.com/course/x/",
        "42",
        [_chapter("S", 1), _lecture("2", "Lectura", "Article")],
    )
    return course.chapters[0].units[0]


@pytest.mark.asyncio
async def test_resolve_extras_articulo_sin_suplementos_expone_body_y_cierra() -> None:
    response = AsyncMock()
    response.text.return_value = json.dumps(
        {
            "asset": {"asset_type": "Article", "body": "<p>Test reading</p>"},
            "supplementary_assets": [],
        }
    )
    extractor = UdemyExtractor()
    client = AsyncMock()
    client.get.return_value = response
    extractor._client = client

    extras = await extractor.resolve_extras(None, _article_unit())

    assert extras.summary_html == "<p>Test reading</p>"
    assert extras.resources == []
    response.close.assert_awaited_once_with()
    requested_url = client.get.await_args.args[0]
    assert "fields%5Blecture%5D=asset%2Csupplementary_assets" in requested_url
    assert "body" in requested_url


@pytest.mark.asyncio
async def test_resolve_extras_articulo_con_archivo_y_enlace_preserva_todo() -> None:
    response = AsyncMock()
    response.text.return_value = json.dumps(
        {
            "asset": {"asset_type": "Article", "body": "<p>Test reading</p>"},
            "supplementary_assets": [
                {
                    "asset_type": "File",
                    "filename": "notes.pdf",
                    "download_urls": {
                        "File": [{"file": "https://att-c.udemycdn.com/test/notes.pdf"}]
                    },
                },
                {
                    "asset_type": "ExternalLink",
                    "title": "Reference",
                    "external_url": "https://example.test/reference",
                },
            ],
        }
    )
    extractor = UdemyExtractor()
    client = AsyncMock()
    client.get.return_value = response
    extractor._client = client

    extras = await extractor.resolve_extras(None, _article_unit())

    assert extras.summary_html == "<p>Test reading</p>"
    assert [resource.kind for resource in extras.resources] == [
        ResourceKind.FILE,
        ResourceKind.LINK,
    ]
    response.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_resolve_extras_cierra_respuesta_si_falla_lectura() -> None:
    response = AsyncMock()
    response.text.side_effect = RuntimeError("synthetic read failure")
    extractor = UdemyExtractor()
    client = AsyncMock()
    client.get.return_value = response
    extractor._client = client

    extras = await extractor.resolve_extras(None, _article_unit())

    assert extras.summary_html is None
    assert extras.resources == []
    response.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_fetch_text_cierra_respuesta_si_falla_lectura() -> None:
    response = MagicMock()
    response.status_code.as_int.return_value = 200
    response.text = AsyncMock(side_effect=RuntimeError("synthetic read failure"))
    response.close = AsyncMock()
    extractor = UdemyExtractor()
    client = AsyncMock()
    client.get.return_value = response
    extractor._client = client

    assert await extractor._fetch_text("https://www.udemy.com/course/x/") == ""
    response.close.assert_awaited_once_with()


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


def test_drm_refresher_runs_fetch_on_original_loop() -> None:
    """The worker-thread callback schedules Udemy I/O on the source loop."""
    ex = UdemyExtractor()
    current = DrmInfo(scheme="widevine", token="old-token")
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def fetch_drm_asset(course_id: str, lecture_id: str) -> dict:
        observed_loops.append(asyncio.get_running_loop())
        return _drm_asset("fresh-token")

    async def exercise() -> None:
        source_loop = asyncio.get_running_loop()
        ex._fetch_drm_asset = fetch_drm_asset  # type: ignore[method-assign]
        refresh = ex._build_drm_refresher("course", "lecture", current)
        refreshed = await asyncio.to_thread(lambda: asyncio.run(refresh()))
        assert refreshed is not None
        assert refreshed.token == "fresh-token"
        assert observed_loops == [source_loop]

    asyncio.run(exercise())


def test_drm_refresher_retries_missing_token_then_succeeds() -> None:
    """A missing token is retried with bounded exponential backoff."""
    ex = UdemyExtractor()
    current = DrmInfo(scheme="widevine", token="old-token")
    fetch = AsyncMock(side_effect=[{}, {"media_license_token": "fresh-token"}])
    ex._fetch_drm_asset = fetch  # type: ignore[method-assign]

    async def exercise() -> None:
        refresh = ex._build_drm_refresher("course", "lecture", current)
        refreshed = await refresh()
        assert refreshed.token == "fresh-token"

    with patch("evdownloader.extractors.udemy.asyncio.sleep", new_callable=AsyncMock) as sleep:
        asyncio.run(exercise())

    assert fetch.await_count == 2
    sleep.assert_awaited_once_with(1.0)


def test_drm_refresher_raises_after_transient_failures_and_empty_assets() -> None:
    """Exceptions and incomplete assets do not fall back to the old token."""
    ex = UdemyExtractor()
    current = DrmInfo(scheme="widevine", token="old-token")
    fetch = AsyncMock(side_effect=[RuntimeError("temporary"), {}, {"media_license_token": ""}])
    ex._fetch_drm_asset = fetch  # type: ignore[method-assign]

    async def exercise() -> None:
        refresh = ex._build_drm_refresher("course", "lecture", current)
        with pytest.raises(ValueError, match="fresh DRM media license token"):
            await refresh()

    with patch("evdownloader.extractors.udemy.asyncio.sleep", new_callable=AsyncMock) as sleep:
        asyncio.run(exercise())

    assert fetch.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [1.0, 2.0]


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
    ex.configure(
        Settings(
            cookies_from_browser="brave",
            use_drm=True,
            drm_license_server="https://my-server.com/license",
        )
    )
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
    ex.configure(
        Settings(
            cookies_from_browser="brave",
            use_drm=True,
            drm_token="cli-token-override",
        )
    )
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


# -- verify_session distingue sesión real de invitado -------------------------
_UDEMY_COOKIE = {"name": "access_token", "value": "tok", "domain": ".udemy.com"}


def _verify_client(body: str) -> AsyncMock:
    response = AsyncMock()
    response.text.return_value = body
    client = AsyncMock()
    client.get.return_value = response
    return client


@pytest.mark.asyncio
async def test_verify_session_acepta_usuario_autenticado() -> None:
    ex = UdemyExtractor()
    client = _verify_client(json.dumps({"_class": "user", "id": 727331}))
    ex._client = client
    assert await ex.verify_session([_UDEMY_COOKIE]) is True
    client.get.return_value.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_verify_session_rechaza_token_invitado() -> None:
    # Respuesta 403 de Udemy: dict sin usuario -> sesión anónima.
    ex = UdemyExtractor()
    client = _verify_client(json.dumps({"detail": "No tienes permiso."}))
    client.get.return_value.close.side_effect = RuntimeError("synthetic close failure")
    ex._client = client
    assert await ex.verify_session([_UDEMY_COOKIE]) is False
    client.get.return_value.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_verify_session_cierra_respuesta_con_json_invalido() -> None:
    ex = UdemyExtractor()
    client = _verify_client("not-json")
    ex._client = client
    assert await ex.verify_session([_UDEMY_COOKIE]) is False
    client.get.return_value.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_verify_session_cierra_respuesta_si_falla_lectura() -> None:
    ex = UdemyExtractor()
    client = _verify_client("")
    client.get.return_value.text.side_effect = RuntimeError("synthetic read failure")
    ex._client = client
    assert await ex.verify_session([_UDEMY_COOKIE]) is False
    client.get.return_value.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_verify_session_sin_cookies_udemy_es_falsa() -> None:
    ex = UdemyExtractor()
    ex._client = _verify_client(json.dumps({"_class": "user", "id": 1}))
    # Cookie de otro dominio: no se construye header y ni siquiera se consulta.
    foreign = [{"name": "access_token", "value": "x", "domain": ".google.com"}]
    assert await ex.verify_session(foreign) is False


@pytest.mark.asyncio
async def test_verify_session_falla_cerrado_ante_error() -> None:
    ex = UdemyExtractor()
    client = AsyncMock()
    client.get.side_effect = RuntimeError("boom")
    ex._client = client
    assert await ex.verify_session([_UDEMY_COOKIE]) is False
