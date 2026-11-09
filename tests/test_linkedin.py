"""Offline contracts; fixtures contain synthetic, non-account data only."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from typer.testing import CliRunner

from evdownloader import browser, service
from evdownloader.cli import app
from evdownloader.config import Settings
from evdownloader.extractors import get_extractor, get_extractor_by_name
from evdownloader.extractors.linkedin import LinkedInLearningError, LinkedInLearningExtractor
from evdownloader.models import Subtitle, Unit, VideoSource

COURSE = "https://www.linkedin.com/learning/example-course"
LESSON = f"{COURSE}/first"
CATALOG = {
    "title": "Example course",
    "chapters": [
        {"title": "Introduction", "videos": [{"slug": "first", "title": "First"}]},
        {"title": "Next", "videos": [{"slug": "second", "title": "Second"}]},
    ],
}
VIDEO = {
    "url": {"progressiveUrl": "https://dms.licdn.com/video/example.mp4"},
    "durationInSeconds": 10,
    "transcript": {
        "language": "es",
        "lines": [
            {"transcriptStartAt": 0, "caption": "Primero"},
            {"transcriptStartAt": 2000, "caption": "Segundo"},
        ],
    },
}


def cookies() -> list[dict]:
    return [
        {"name": "li_at", "value": "synthetic-session", "domain": ".linkedin.com", "secure": True},
        {
            "name": "JSESSIONID",
            "value": '"ajax:synthetic"',
            "domain": ".linkedin.com",
            "secure": True,
        },
        {"name": "unrelated", "value": "synthetic", "domain": ".example.org"},
    ]


@pytest.fixture
def extractor():
    with patch("evdownloader.browser.resolve_cookies", side_effect=lambda *args: cookies()):
        yield LinkedInLearningExtractor()


@pytest.mark.parametrize("url", [COURSE, LESSON, COURSE + "/", LESSON + "?autoplay=true"])
def test_registration(url):
    assert isinstance(get_extractor(url), LinkedInLearningExtractor)
    assert isinstance(get_extractor_by_name("linkedin"), LinkedInLearningExtractor)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.linkedin.com/learning/example",
        "https://linkedin.com.evil.test/learning/example",
        "https://evil-linkedin.com/learning/example",
        "https://www.linkedin.com@evil.test/learning/example",
        "https://evil.test@www.linkedin.com/learning/example",
        "https://www.linkedin.com:444/learning/example",
        "https://www.linkedin.com./learning/example",
        "https://sub.linkedin.com/learning/example",
        "https://www.linkedin.com/posts/example",
        COURSE + "/a/b",
        COURSE + "/%2e%2e",
        COURSE + "#fragment",
        "https://www.linkedin.com/learning/",
        "https://www.linkedin.com/learning/%2fexample",
    ],
)
def test_unsafe_urls_rejected_before_cookies(url, extractor):
    assert not extractor.supports(url)


@pytest.mark.asyncio
async def test_rejected_url_makes_no_auth_or_http_request(extractor):
    with patch.object(extractor, "_api", new_callable=AsyncMock) as api:
        with pytest.raises(LinkedInLearningError):
            await extractor.list_course(None, "https://evil.test/learning/example")
        api.assert_not_awaited()


@pytest.mark.asyncio
async def test_listing_preserves_provider_order_and_normalizes_lesson_input(extractor):
    with patch.object(extractor, "_api", new=AsyncMock(return_value=CATALOG)) as api:
        result = await extractor.list_course(None, LESSON)
    api.assert_awaited_once_with("example-course", "chapters,description,title")
    assert result.url == COURSE
    assert [c.index for c in result.chapters] == [1, 2]
    assert [u.index for c in result.chapters for u in c.units] == [1, 2]
    assert [u.title for c in result.chapters for u in c.units] == ["First", "Second"]
    assert all(u.video is None for c in result.chapters for u in c.units)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"chapters": []},
        {"title": "T", "chapters": [None]},
        {"title": "T", "chapters": [{"title": "C", "videos": []}]},
        {"title": "T", "chapters": [{"title": "C", "videos": [None]}]},
        {
            "title": "T",
            "chapters": [{"title": "C", "videos": [{"slug": "../escape", "title": "T"}]}],
        },
        {"title": "T", "chapters": [{"title": "C", "videos": [{"slug": "first"}]}]},
    ],
)
async def test_listing_schema_failures(extractor, data):
    with (
        patch.object(extractor, "_api", new=AsyncMock(return_value=data)),
        pytest.raises(LinkedInLearningError),
    ):
        await extractor.list_course(None, COURSE)


@pytest.mark.asyncio
async def test_duplicate_lessons_are_not_silently_dropped(extractor):
    data = copy.deepcopy(CATALOG)
    data["chapters"][1]["videos"][0]["slug"] = "first"
    with (
        patch.object(extractor, "_api", new=AsyncMock(return_value=data)),
        pytest.raises(LinkedInLearningError),
    ):
        await extractor.list_course(None, COURSE)


@pytest.mark.parametrize(
    "change", ["missing", "expired", "wrong_path", "wrong_host", "csrf_absent"]
)
def test_authentication_scope_and_expiry(change):
    raw = cookies()[:2]
    if change == "missing":
        raw = []
    elif change == "csrf_absent":
        raw = raw[:1]
    else:
        for cookie in raw:
            cookie.update(
                {
                    "expired": {"expires": 1},
                    "wrong_path": {"path": "/other"},
                    "wrong_host": {"domain": ".example.org"},
                }[change]
            )
    with (
        patch("evdownloader.browser.resolve_cookies", return_value=raw),
        pytest.raises(LinkedInLearningError),
    ):
        LinkedInLearningExtractor()._headers(
            "https://www.linkedin.com/learning-api/detailedCourses"
        )


def test_cookie_filtering_and_csrf_normalization(extractor):
    headers = extractor._headers("https://www.linkedin.com/learning-api/detailedCourses")
    assert headers["Csrf-Token"] == "ajax:synthetic"
    assert "unrelated" not in headers["Cookie"]
    assert {c.domain for c in extractor._cookie_records()} == {".linkedin.com"}
    assert browser.filter_cookies("linkedin", cookies()) == cookies()[:2]
    assert browser.has_usable_session("linkedin", cookies())
    assert not browser.has_usable_session("linkedin", cookies()[1:])
    assert (
        browser.cookie_header_for_url(
            extractor._cookie_records(),
            "https://dms.licdn.com/video.mp4",
            trusted_host_suffixes=extractor.resource_host_suffixes,
        )
        is None
    )


def response(status=200, payload=None):
    result = MagicMock()
    result.status_code.as_int.return_value = status
    result.text = AsyncMock(
        return_value=json.dumps(payload if payload is not None else {"elements": [CATALOG]})
    )
    result.close = AsyncMock()
    return result


@pytest.mark.asyncio
async def test_api_contract_and_no_redirect_following(extractor):
    reply = response()
    client = MagicMock(get=AsyncMock(return_value=reply))
    with patch("evdownloader.extractors.linkedin.rnet.Client", return_value=client):
        assert await extractor._api("example-course", "selectedVideo", "first") == CATALOG
    args, kwargs = client.get.await_args
    assert parse_qs(urlsplit(args[0]).query) == {
        "courseSlug": ["example-course"],
        "fields": ["selectedVideo"],
        "q": ["slugs"],
        "videoSlug": ["first"],
        "resolution": ["_720"],
    }
    assert kwargs["allow_redirects"] is False
    reply.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 429, 500, 999])
async def test_api_failure_sanitized_and_not_retried(extractor, status):
    reply = response(status)
    reply.headers = {"location": "https://evil.test/private?token=synthetic"}
    client = MagicMock(get=AsyncMock(return_value=reply))
    with (
        patch("evdownloader.extractors.linkedin.rnet.Client", return_value=client),
        pytest.raises(LinkedInLearningError) as error,
    ):
        await extractor._api("example-course", "title")
    assert "synthetic" not in str(error.value)
    assert "evil.test" not in str(error.value)
    client.get.assert_awaited_once()
    reply.text.assert_not_awaited()
    reply.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [[], {}, {"elements": []}, {"elements": [None]}, {"elements": [1, 2]}]
)
async def test_api_unknown_schema_fails(extractor, payload):
    reply = response(payload=payload)
    with (
        patch(
            "evdownloader.extractors.linkedin.rnet.Client",
            return_value=MagicMock(get=AsyncMock(return_value=reply)),
        ),
        pytest.raises(LinkedInLearningError),
    ):
        await extractor._api("example-course", "title")
    reply.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_transport_and_browser_errors_do_not_leak_secrets(extractor):
    with (
        patch(
            "evdownloader.extractors.linkedin.rnet.Client",
            return_value=MagicMock(
                get=AsyncMock(side_effect=RuntimeError("synthetic-private-url"))
            ),
        ),
        pytest.raises(LinkedInLearningError) as error,
    ):
        await extractor._api("example-course", "title")
    assert "synthetic-private" not in str(error.value)
    with (
        patch(
            "evdownloader.browser.resolve_cookies",
            side_effect=RuntimeError("synthetic-private-profile"),
        ),
        pytest.raises(LinkedInLearningError) as error,
    ):
        LinkedInLearningExtractor()._cookie_records()
    assert "synthetic-private" not in str(error.value)


@pytest.mark.asyncio
async def test_resolve_media_and_subtitles_reuse_one_metadata_request(extractor):
    with patch.object(
        extractor, "_api", new=AsyncMock(return_value={"selectedVideo": VIDEO})
    ) as api:
        source = await extractor.resolve_video(None, Unit(title="First", url=LESSON))
        source_again = await extractor.resolve_video(None, Unit(title="First", url=LESSON))
    api.assert_awaited_once()
    assert source.url == VIDEO["url"]["progressiveUrl"]
    assert source.subtitle_inventory_confirmed
    assert source.subtitles[0].content.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:02.000")
    assert source.subtitles[0].lang == "es"
    assert source.drm is None and not source.write_subs
    assert source_again.url == source.url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"url": {}},
        {"url": {"progressiveUrl": "https://evil.test/private"}},
        {**VIDEO, "isDrmProtected": True},
    ],
)
async def test_no_media_or_unsafe_or_drm_is_not_success(extractor, data):
    with (
        patch.object(extractor, "_video", new=AsyncMock(return_value=data)),
        pytest.raises(LinkedInLearningError),
    ):
        await extractor.resolve_video(None, Unit(title="First", url=LESSON))


def unpublished(duration: int = 146) -> dict:
    return {
        **VIDEO,
        "public": False,
        "androidPublic": False,
        "durationInSeconds": duration,
    }


@pytest.mark.asyncio
async def test_unpublished_placeholder_rendition_is_reported(extractor):
    extractor._course_slug = "example-course"
    with (
        patch.object(extractor, "_video", new=AsyncMock(return_value=unpublished())),
        patch.object(extractor, "_progressive_bitrate_kbps", new=AsyncMock(return_value=197.0)),
        pytest.raises(LinkedInLearningError) as error,
    ):
        await extractor.resolve_video(None, Unit(title="First", url=LESSON))
    message = str(error.value)
    assert "no está publicando el video real" in message
    assert "example-course" in message
    assert "197 kbps" in message
    assert "public=False" in message
    assert "androidPublic=False" in message


@pytest.mark.asyncio
async def test_unpublished_flag_with_real_bitrate_still_downloads(extractor):
    probe = AsyncMock(return_value=1240.0)
    with (
        patch.object(extractor, "_video", new=AsyncMock(return_value=unpublished())),
        patch.object(extractor, "_progressive_bitrate_kbps", new=probe),
    ):
        source = await extractor.resolve_video(None, Unit(title="First", url=LESSON))
    probe.assert_awaited_once()
    assert source.url == VIDEO["url"]["progressiveUrl"]


@pytest.mark.asyncio
async def test_published_media_skips_the_size_probe(extractor):
    data = {**VIDEO, "public": True, "androidPublic": True, "durationInSeconds": 146}
    probe = AsyncMock(return_value=1.0)
    with (
        patch.object(extractor, "_video", new=AsyncMock(return_value=data)),
        patch.object(extractor, "_progressive_bitrate_kbps", new=probe),
    ):
        await extractor.resolve_video(None, Unit(title="First", url=LESSON))
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmeasurable_size_never_blocks_a_download(extractor):
    with (
        patch.object(extractor, "_video", new=AsyncMock(return_value=unpublished())),
        patch.object(extractor, "_progressive_bitrate_kbps", new=AsyncMock(return_value=None)),
    ):
        source = await extractor.resolve_video(None, Unit(title="First", url=LESSON))
    assert source.url == VIDEO["url"]["progressiveUrl"]


@pytest.mark.asyncio
async def test_bitrate_probe_parses_bytes_content_length(extractor):
    response = MagicMock()
    response.status_code.as_int.return_value = 200
    response.headers = {"content-length": b"1000000"}
    response.close = AsyncMock()
    client = MagicMock()
    client.head = AsyncMock(return_value=response)
    with patch("evdownloader.extractors.linkedin.rnet.Client", return_value=client):
        kbps = await extractor._progressive_bitrate_kbps("https://dms.licdn.com/v.mp4", 10)
    assert kbps == pytest.approx(800.0)


@pytest.mark.asyncio
async def test_cross_course_and_missing_selected_video_rejected(extractor):
    extractor._course_slug = "other"
    with patch.object(extractor, "_api", new_callable=AsyncMock) as api:
        with pytest.raises(LinkedInLearningError):
            await extractor._video(Unit(title="First", url=LESSON))
        api.assert_not_awaited()
    extractor._course_slug = None
    with (
        patch.object(extractor, "_api", new=AsyncMock(return_value={})),
        pytest.raises(LinkedInLearningError),
    ):
        await extractor._video(Unit(title="First", url=LESSON))


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["exerciseFiles", "resources", "attachments", "absent"])
async def test_advertised_unsupported_resources_fail_truthfully(extractor, key):
    with (
        patch.object(
            extractor, "_video", new=AsyncMock(return_value={key: [{"unknown": "value"}]})
        ),
        pytest.raises(LinkedInLearningError, match="recursos"),
    ):
        await extractor.resolve_extras(None, Unit(title="First", url=LESSON))


@pytest.mark.parametrize(
    "transcript",
    [
        "invalid",
        {"lines": None},
        {"lines": [None]},
        {"lines": [{"transcriptStartAt": -1, "caption": "T"}]},
        {
            "lines": [
                {"transcriptStartAt": 0, "caption": "T"},
                {"transcriptStartAt": 0, "caption": "T"},
            ]
        },
        {"lines": [{"transcriptStartAt": True, "caption": "T"}]},
    ],
)
def test_transcript_schema_and_order(transcript):
    with pytest.raises(LinkedInLearningError):
        LinkedInLearningExtractor._subtitles({"transcript": transcript, "durationInSeconds": 10})


def test_transcript_language_is_not_guessed_and_absence_is_explicit():
    data = copy.deepcopy(VIDEO)
    del data["transcript"]["language"]
    assert LinkedInLearningExtractor._subtitles(data)[0].lang == "und"
    assert LinkedInLearningExtractor._subtitles({"transcript": None}) == []
    assert LinkedInLearningExtractor._subtitles({"transcript": {"lines": []}}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "media", "inventory", "publication"])
async def test_existing_engine_and_truthful_artifact_results(extractor, tmp_path, failure):
    data = copy.deepcopy(VIDEO)
    if failure == "inventory":
        del data["transcript"]
    if failure == "media":
        data["url"] = {}

    async def download(source, base, settings):
        path = Path(str(base) + ".mp4")
        path.write_bytes(b"synthetic-media")
        return path

    engine = MagicMock(download=AsyncMock(side_effect=download))
    publish = service.publish_atomic_text

    def publish_except_vtt(path, content):
        if path.suffix == ".vtt":
            raise OSError
        return publish(path, content)

    with patch.object(extractor, "_video", new=AsyncMock(return_value=data)):
        if failure == "publication":
            with patch("evdownloader.service.publish_atomic_text", side_effect=publish_except_vtt):
                outcome = await service._download_video(
                    extractor,
                    engine,
                    None,
                    Unit(title="First", url=LESSON),
                    tmp_path / "First",
                    Settings(),
                )
        else:
            outcome = await service._download_video(
                extractor,
                engine,
                None,
                Unit(title="First", url=LESSON),
                tmp_path / "First",
                Settings(),
            )
    assert outcome.video_ok is (failure != "media")
    assert outcome.subtitles_ok is (failure is None)
    assert engine.download.await_count == (0 if failure == "media" else 1)
    if failure is None:
        assert (tmp_path / "First.es.vtt").read_text().startswith("WEBVTT")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content", ["WEBVTT\n\n", "not vtt", "WEBVTT\n" + "x" * (10 * 1024 * 1024 + 1)]
)
async def test_inline_subtitles_use_existing_validation_without_http(tmp_path, content):
    with patch("evdownloader.service._fetch_subtitle", new_callable=AsyncMock) as fetch:
        report = await service._save_subtitles(
            [Subtitle(lang="und", url="", content=content)],
            tmp_path / "inline",
            VideoSource(url="https://dms.licdn.com/a.mp4"),
        )
    fetch.assert_not_awaited()
    assert bool(report.failures) is (
        not content.startswith("WEBVTT") or len(content) > 10 * 1024 * 1024
    )


def test_cli_sanitized_provider_error_without_traceback():
    with patch(
        "evdownloader.service.download_course",
        new=AsyncMock(side_effect=LinkedInLearningError("Safe provider error")),
    ):
        result = CliRunner().invoke(app, ["download", COURSE, "--no-resources", "--limit", "1"])
    assert result.exit_code == 1
    assert "Safe provider error" in result.output
    assert "Traceback" not in result.output


def test_cli_linkedin_login_does_not_open_browser():
    with patch("evdownloader.cli.session.login", new_callable=AsyncMock) as login:
        result = CliRunner().invoke(app, ["login", "linkedin"])
    assert result.exit_code == 2
    assert "cookies-from-browser" in result.output
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_auth_never_constructs_http_client():
    with (
        patch("evdownloader.browser.resolve_cookies", return_value=[]),
        patch("evdownloader.extractors.linkedin.rnet.Client") as client,
        pytest.raises(LinkedInLearningError),
    ):
        await LinkedInLearningExtractor()._api("example-course", "title")
    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("resources", [False, True])
async def test_course_orchestration_respects_limit_and_resource_opt_out(
    extractor, tmp_path, resources
):
    async def api(course, fields, video=None):
        return CATALOG if fields.startswith("chapters") else {"selectedVideo": VIDEO}

    async def download(source, base, settings):
        path = Path(str(base) + ".mp4")
        path.write_bytes(b"synthetic-media")
        return path

    engine = MagicMock(download=AsyncMock(side_effect=download))
    with (
        patch.object(extractor, "_api", new=AsyncMock(side_effect=api)),
        patch("evdownloader.service.cache.set"),
    ):
        result = await service._run_download(
            extractor,
            engine,
            None,
            LESSON,
            Settings(download_dir=tmp_path, limit=1, resources=resources),
            use_cache=False,
        )
    assert result.incomplete is resources
    assert result.extras_failures == int(resources)
    assert result.video_failures == result.subtitle_failures == 0
    assert len(list(tmp_path.rglob("*.mp4"))) == 1
    assert len(list(tmp_path.rglob("*.vtt"))) == 1
    engine.download.assert_awaited_once()


def test_existing_ytdlp_engine_suppresses_signed_diagnostics(tmp_path, capsys):
    import yt_dlp

    from evdownloader.downloaders.ytdlp import YtDlpDownloader

    opts = YtDlpDownloader._common_options(
        VideoSource(url="https://dms.licdn.com/example.mp4"), tmp_path / "example", Settings()
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.report_warning("https://dms.licdn.com/private?signature=synthetic")
        with pytest.raises(yt_dlp.utils.DownloadError):
            ydl.report_error("https://dms.licdn.com/private?signature=synthetic")
    captured = capsys.readouterr()
    assert "synthetic" not in captured.out + captured.err
