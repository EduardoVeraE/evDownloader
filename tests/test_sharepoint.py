from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import typer
from typer.testing import CliRunner

from evdownloader import browser, cli
from evdownloader.config import Settings
from evdownloader.extractors import get_extractor
from evdownloader.extractors.sharepoint import (
    SharePointAuthError,
    SharePointDrmError,
    SharePointEmptyError,
    SharePointExtractor,
    SharePointResponseError,
    SharePointTrustError,
)
from evdownloader.models import Cookie, DrmInfo, Unit, UnitType


class _Response:
    def __init__(
        self,
        status: int,
        payload: object | None = None,
        *,
        text: str | None = None,
        location: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = SimpleNamespace(
            as_int=lambda: status, is_success=lambda: 200 <= status < 300
        )
        self.headers = headers or ({"location": location} if location else {})
        self.text = AsyncMock(return_value=text if text is not None else json.dumps(payload))
        self.close = AsyncMock()


def _extractor(*responses: _Response) -> SharePointExtractor:
    extractor = SharePointExtractor()
    extractor._cookie_jar = [
        Cookie(name="FedAuth", value="auth", domain=".sharepoint.com", secure=True)
    ]
    client = SimpleNamespace(get=AsyncMock(side_effect=list(responses)))
    extractor._client = client
    return extractor


def _file(
    name: str,
    path: str,
    title: str | None = None,
) -> dict[str, object]:
    fields: dict[str, object] = {}
    if title is not None:
        fields["Title"] = title
    return {"Name": name, "ServerRelativeUrl": path, "ListItemAllFields": fields}


def test_folder_path_rejects_stream_file_and_decodes_safe_server_path() -> None:
    assert SharePointExtractor._folder_path(
        "https://tenant.sharepoint.com/Documents/Grabaciones%20Finales"
    ) == "/Documents/Grabaciones Finales"
    with pytest.raises(ValueError, match="folder"):
        SharePointExtractor._folder_path(
            "https://tenant.sharepoint.com/_layouts/15/stream.aspx?id=%2FDocuments%2Fa.mp4"
        )
    with pytest.raises(ValueError, match="query"):
        SharePointExtractor._folder_path("https://tenant.sharepoint.com/Documents/Folder?x=1")
    with pytest.raises(ValueError, match="folder"):
        SharePointExtractor._folder_path("https://tenant.sharepoint.com/Documents/../Secrets")


def test_configure_rejects_unsupported_cookie_source() -> None:
    extractor = SharePointExtractor()
    with pytest.raises(ValueError, match="Chrome or Edge"):
        extractor.configure(Settings(cookies_from_browser="firefox"))


def test_login_rejects_sharepoint_manual_authentication() -> None:
    result = CliRunner().invoke(cli.app, ["login", "sharepoint"])

    assert result.exit_code != 0
    assert "cookies-from-browser" in result.output
    with pytest.raises(typer.BadParameter, match="cookies-from-browser"):
        cli.login("sharepoint")


@pytest.mark.asyncio
async def test_list_course_enumerates_pages_filters_and_groups_deterministically() -> None:
    first = _Response(
        200,
        {
            "value": [
                _file("b.mp4", "/Documents/b.mp4", "  Intro  "),
                _file("notes.pdf", "/Documents/notes.pdf", "Intro"),
                _file("a.mp4", "/Documents/a.mp4", "INTRO"),
            ],
            "@odata.nextLink": "https://tenant.sharepoint.com/_api/web/next?page=2",
        },
    )
    second = _Response(200, {"value": [_file("c.webm", "/Documents/c.webm", "Advanced")]})
    course = await _extractor(first, second).list_course(
        None, "https://tenant.sharepoint.com/Documents"
    )

    assert course.title == "Documents"
    assert [(chapter.title, len(chapter.units)) for chapter in course.chapters] == [
        ("Advanced", 1),
        ("INTRO", 2),
    ]
    assert [unit.url for unit in course.chapters[1].units] == [
        "https://tenant.sharepoint.com/Documents/a.mp4",
        "https://tenant.sharepoint.com/Documents/b.mp4",
    ]
    assert [unit.index for chapter in course.chapters for unit in chapter.units] == [1, 2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (_Response(401, {"error": "unauthorized"}), SharePointAuthError),
        (_Response(403, {"error": "forbidden"}), SharePointAuthError),
        (_Response(200, text="<html>Sign in to your account</html>"), SharePointAuthError),
        (_Response(200, text="not json"), SharePointResponseError),
    ],
)
async def test_list_course_fails_closed_on_auth_or_malformed_responses(
    response: _Response, error: type[Exception]
) -> None:
    with pytest.raises(error):
        await _extractor(response).list_course(None, "https://tenant.sharepoint.com/Documents")


@pytest.mark.asyncio
async def test_list_course_rejects_empty_folder_and_unsafe_pagination() -> None:
    with pytest.raises(SharePointEmptyError, match="recordings"):
        await _extractor(_Response(200, {"value": []})).list_course(
            None, "https://tenant.sharepoint.com/Documents"
        )

    unsafe = _Response(
        200,
        {
            "value": [_file("a.mp4", "/Documents/a.mp4")],
            "@odata.nextLink": "https://evil.test/_api/web/next",
        },
    )
    with pytest.raises(SharePointTrustError, match="pagination"):
        await _extractor(unsafe).list_course(None, "https://tenant.sharepoint.com/Documents")


@pytest.mark.asyncio
async def test_list_course_rejects_unsafe_redirect_before_sending_cookie() -> None:
    redirect = _Response(
        302,
        location="https://evil.test/_api/web/GetFolderByServerRelativeUrl('/Documents')/Files",
    )
    extractor = _extractor(redirect)
    with pytest.raises(SharePointTrustError, match="redirect"):
        await extractor.list_course(None, "https://tenant.sharepoint.com/Documents")
    headers = extractor._client.get.await_args.kwargs["headers"]
    assert "Cookie" in headers
    assert headers["Cookie"] == "FedAuth=auth"
    redirect.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_list_course_follows_trusted_redirect_with_scoped_cookie() -> None:
    redirect = _Response(
        302,
        location="https://api.sharepoint.com/_api/web/next?page=2",
    )
    final = _Response(200, {"value": [_file("recording.mp4", "/Documents/recording.mp4")]})
    extractor = _extractor(redirect, final)

    course = await extractor.list_course(None, "https://tenant.sharepoint.com/Documents")

    assert course.chapters[0].units[0].url.endswith("/Documents/recording.mp4")
    assert extractor._client.get.await_count == 2
    for call in extractor._client.get.await_args_list:
        assert call.kwargs["headers"]["Cookie"] == "FedAuth=auth"
    final.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_resolve_video_returns_scoped_cookie_source() -> None:
    extractor = SharePointExtractor()
    extractor._cookie_jar = [
        Cookie(name="FedAuth", value="auth", domain=".sharepoint.com", secure=True),
        Cookie(name="other", value="no", domain=".evil.test", secure=True),
    ]
    extractor._probe_drm = AsyncMock(return_value=None)
    source = await extractor.resolve_video(
        None,
        Unit(
            title="Recording",
            url="https://tenant.sharepoint.com/Documents/recording.mp4",
            type=UnitType.VIDEO,
            index=1,
        ),
    )

    assert source is not None
    assert source.url.endswith("recording.mp4")
    assert [cookie.name for cookie in source.cookie_jar] == ["FedAuth"]
    assert source.trusted_host_suffixes == ("sharepoint.com",)
    assert source.is_embed is False


@pytest.mark.asyncio
async def test_browser_cookies_reach_rest_and_direct_media_requests(monkeypatch) -> None:
    browser_cookies = [
        {
            "name": "FedAuth",
            "value": "browser-auth",
            "domain": ".sharepoint.com",
            "path": "/",
            "secure": True,
            "expires": 0,
        },
        {
            "name": "rtFa",
            "value": "browser-refresh",
            "domain": "tenant.sharepoint.com",
            "path": "/",
            "secure": True,
            "expires": 0,
        },
        {
            "name": "sid",
            "value": "off-host",
            "domain": ".evil.test",
            "path": "/",
            "secure": True,
            "expires": 0,
        },
    ]
    monkeypatch.setattr(browser, "load_browser_cookies", lambda name: browser_cookies)
    extractor = SharePointExtractor()
    extractor.configure(Settings(cookies_from_browser="chrome"))
    rest = _Response(200, {"value": [_file("recording.mp4", "/Documents/recording.mp4")]})
    media = _Response(200, text="clear media", headers={"content-type": "video/mp4"})
    extractor._client = SimpleNamespace(get=AsyncMock(side_effect=[rest, media]))

    course = await extractor.list_course(None, "https://tenant.sharepoint.com/Documents")
    source = await extractor.resolve_video(None, course.chapters[0].units[0])

    assert source is not None
    assert [cookie.name for cookie in source.cookie_jar] == ["FedAuth", "rtFa"]
    for call in extractor._client.get.await_args_list:
        assert call.kwargs["headers"]["Cookie"] == "FedAuth=browser-auth; rtFa=browser-refresh"


@pytest.mark.asyncio
async def test_direct_file_drm_header_is_reported_before_download() -> None:
    extractor = _extractor(
        _Response(200, text="encrypted media", headers={"X-DRM-System": "PlayReady"})
    )

    with pytest.raises(SharePointDrmError, match="encrypted|decryption"):
        await extractor.resolve_video(
            None,
            Unit(
                title="Protected",
                url="https://tenant.sharepoint.com/Documents/protected.mp4",
                type=UnitType.VIDEO,
            ),
        )


@pytest.mark.asyncio
async def test_direct_file_drm_json_metadata_is_reported_before_download(tmp_path: Path) -> None:
    extractor = _extractor(
        _Response(200, text=json.dumps({"drm": {"scheme": "widevine"}}))
    )
    extractor._select_cdm = lambda: tmp_path / "test-device.wvd"

    source = await extractor.resolve_video(
        None,
        Unit(
            title="Protected",
            url="https://tenant.sharepoint.com/Documents/protected.mp4",
            type=UnitType.VIDEO,
        ),
    )

    assert source is not None
    assert source.drm == DrmInfo(scheme="widevine")


def test_recordings_reject_path_traversal_from_rest_metadata() -> None:
    with pytest.raises(SharePointResponseError, match="path"):
        SharePointExtractor._recordings(
            "https://tenant.sharepoint.com/Documents",
            [_file("protected.mp4", "/Documents/../Secrets/protected.mp4")],
        )


def test_recordings_reject_file_outside_requested_folder() -> None:
    with pytest.raises(SharePointResponseError, match="path"):
        SharePointExtractor._recordings(
            "https://tenant.sharepoint.com/Documents",
            [_file("protected.mp4", "/Secrets/protected.mp4")],
        )


@pytest.mark.asyncio
async def test_resolve_video_rejects_off_host_source() -> None:
    extractor = SharePointExtractor()
    with pytest.raises(SharePointTrustError, match="source"):
        await extractor.resolve_video(
            None, Unit(title="bad", url="https://evil.test/video.mp4", type=UnitType.VIDEO)
        )


def test_select_cdm_uses_first_valid_sorted_wvd_and_preserves_explicit_device(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "01-invalid.wvd").write_bytes(b"invalid-device")
    first = tmp_path / "02-first.wvd"
    second = tmp_path / "03-second.wvd"
    first.write_bytes(b"synthetic-device")
    second.write_bytes(b"synthetic-device-2")
    extractor = SharePointExtractor()
    settings = Settings(cdms_dir=tmp_path)
    extractor.configure(settings)
    monkeypatch.setattr(
        SharePointExtractor,
        "_is_valid_cdm",
        staticmethod(lambda path: path.name != "01-invalid.wvd"),
    )

    assert extractor._select_cdm() == first
    explicit = tmp_path / "explicit.wvd"
    settings.drm_device = explicit
    assert extractor._select_cdm() == explicit


@pytest.mark.asyncio
async def test_widevine_resolution_selects_cdm_and_sets_source_drm(
    monkeypatch, tmp_path: Path
) -> None:
    device = tmp_path / "device.wvd"
    device.write_bytes(b"synthetic-device")
    settings = Settings(cdms_dir=tmp_path, use_drm=True)
    extractor = SharePointExtractor()
    extractor.configure(settings)
    monkeypatch.setattr(SharePointExtractor, "_is_valid_cdm", staticmethod(lambda path: True))
    extractor._cookie_jar = [Cookie(name="FedAuth", value="auth", domain=".sharepoint.com")]
    extractor._probe_drm = AsyncMock(
        return_value=DrmInfo(scheme="widevine", pssh="AAAA", license_url="https://tenant.sharepoint.com/license")
    )
    source = await extractor.resolve_video(
        None, Unit(title="Protected", url="https://tenant.sharepoint.com/protected.mpd")
    )

    assert source is not None and source.drm is not None
    assert source.drm.scheme == "widevine"
    assert settings.drm_device == device


@pytest.mark.asyncio
async def test_drm_probe_rejects_unsafe_manifest_redirect() -> None:
    redirect = _Response(302, location="https://evil.test/protected.mpd")
    extractor = _extractor(redirect)
    with pytest.raises(SharePointTrustError, match="redirect"):
        await extractor._probe_drm("https://tenant.sharepoint.com/protected.mpd")


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["playready", "aes-128"])
async def test_unsupported_drm_reports_encrypted_container_limitation(scheme: str) -> None:
    extractor = SharePointExtractor()
    extractor._cookie_jar = [Cookie(name="FedAuth", value="auth", domain=".sharepoint.com")]
    extractor._probe_drm = AsyncMock(return_value=DrmInfo(scheme=scheme))
    with pytest.raises(SharePointDrmError, match="encrypted|decryption"):
        await extractor.resolve_video(
            None, Unit(title="Protected", url="https://tenant.sharepoint.com/protected.mpd")
        )


def test_sharepoint_is_registered_but_stream_host_is_not() -> None:
    assert isinstance(get_extractor("https://tenant.sharepoint.com/Documents"), SharePointExtractor)
    with pytest.raises(ValueError):
        get_extractor("https://tenant.stream.microsoft.com/Documents")
    assert "sharepoint" in cli._PLATFORMS


@pytest.mark.asyncio
async def test_list_course_requires_scoped_authentication() -> None:
    with pytest.raises(SharePointAuthError, match="cookies"):
        await SharePointExtractor().list_course(None, "https://tenant.sharepoint.com/Documents")
