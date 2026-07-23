import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evdownloader.models import Cookie, Subtitle, VideoSource
from evdownloader.service import _save_subtitles


def response(
    body: bytes = b"WEBVTT\n", *, status: int = 200, headers: dict[str, str] | None = None
) -> MagicMock:
    result = MagicMock()
    result.status_code.is_success.return_value = 200 <= status < 300
    result.status_code.as_int.return_value = status
    result.headers = headers or {}
    result.bytes = AsyncMock(return_value=body)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (
            b"WEBVTT\n\n00:00.000 --> 00:01.000\nOne",
            "text/vtt",
            "WEBVTT\n\n00:00.000 --> 00:01.000\nOne",
        ),
        (b"\xef\xbb\xbfWEBVTT\n", "text/plain", "WEBVTT\n"),
        (b"\xef\xbb\xbf \t\r\nWEBVTT header\n", "application/octet-stream", "WEBVTT header\n"),
        (b" \t\r\nWEBVTT\n", None, "WEBVTT\n"),
    ],
)
async def test_save_subtitles_accepts_valid_body_regardless_of_content_type(
    tmp_path: Path, body: bytes, content_type: str | None, expected: str
) -> None:
    headers = {} if content_type is None else {"content-type": content_type}
    client = MagicMock()
    client.get = AsyncMock(return_value=response(body, headers=headers))
    source = VideoSource(
        url="https://video.example.test/master.m3u8",
        subtitles=[Subtitle(url="https://cdn.example.test/sub.vtt", lang="en")],
    )

    with patch("evdownloader.service.rnet.Client", return_value=client) as client_factory:
        report = await _save_subtitles(source.subtitles, tmp_path / "lesson", source)

    assert client_factory.call_args.kwargs["timeout"] == 30
    assert client_factory.call_args.kwargs["connect_timeout"] == 10
    assert client_factory.call_args.kwargs["read_timeout"] == 30
    assert report.attempted == 1
    assert report.saved_count == 1
    assert report.failures == ()
    assert (tmp_path / "lesson.en.vtt").read_text(encoding="utf-8") == expected


@pytest.mark.asyncio
async def test_save_subtitles_scopes_fresh_cookie_headers_per_url(tmp_path: Path) -> None:
    subtitles = [
        Subtitle(url="https://a.example.test/subs/a.vtt", lang="a"),
        Subtitle(url="https://b.example.test/subs/b.vtt", lang="b"),
        Subtitle(url="https://other.test/subs/c.vtt", lang="c"),
    ]
    client = MagicMock()
    client.get = AsyncMock(side_effect=[response(), response(), response()])
    source = VideoSource(
        url="https://video.example.test/master.m3u8",
        subtitles=subtitles,
        http_headers={"Referer": "https://app.example.test", "cOoKiE": "old=leak"},
        cookies={"flattened": "secret-leak"},
        cookie_jar=[
            Cookie(name="a_session", value="a-secret", domain="a.example.test", path="/subs"),
            Cookie(name="b_session", value="b-secret", domain="b.example.test", path="/subs"),
        ],
    )

    with patch("evdownloader.service.rnet.Client", return_value=client):
        report = await _save_subtitles(subtitles, tmp_path / "lesson", source)

    sent_headers = [call.kwargs["headers"] for call in client.get.await_args_list]
    assert sent_headers == [
        {"Referer": "https://app.example.test", "Cookie": "a_session=a-secret"},
        {"Referer": "https://app.example.test", "Cookie": "b_session=b-secret"},
        {"Referer": "https://app.example.test"},
    ]
    assert len({id(headers) for headers in sent_headers}) == 3
    assert "flattened" not in repr(sent_headers)
    assert "old=leak" not in repr(sent_headers)
    assert report.saved_count == 3


@pytest.mark.asyncio
async def test_save_subtitles_does_not_read_non_2xx_body(tmp_path: Path) -> None:
    forbidden = response(b"secret response body", status=403)
    client = MagicMock()
    client.get = AsyncMock(return_value=forbidden)
    source = VideoSource(
        url="https://example.test/video",
        subtitles=[Subtitle(url="https://example.test/signed.vtt?token=secret", lang="es")],
    )

    with patch("evdownloader.service.rnet.Client", return_value=client):
        report = await _save_subtitles(source.subtitles, tmp_path / "lesson", source)

    forbidden.bytes.assert_not_awaited()
    assert report.saved_count == 0
    assert report.failures[0].reason == "http_status"
    assert report.failures[0].http_status == 403
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_save_subtitles_rejects_invalid_and_oversized_bodies(tmp_path: Path) -> None:
    oversized = b"WEBVTT\n" + b"x" * (10 * 1024 * 1024)
    declared = response(headers={"content-length": str(10 * 1024 * 1024 + 1)})
    responses = [
        response(b"<html>WEBVTT</html>"),
        response(b""),
        response(b"\xffWEBVTT"),
        declared,
        response(oversized),
        response(b"WEBVTT-not-a-boundary"),
    ]
    subtitles = [
        Subtitle(url=f"https://example.test/{index}.vtt", lang="es")
        for index in range(len(responses))
    ]
    client = MagicMock()
    client.get = AsyncMock(side_effect=responses)
    source = VideoSource(url="https://example.test/video", subtitles=subtitles)

    with patch("evdownloader.service.rnet.Client", return_value=client):
        report = await _save_subtitles(subtitles, tmp_path / "lesson", source)

    assert [failure.reason for failure in report.failures] == [
        "invalid_vtt",
        "empty",
        "invalid_utf8",
        "declared_too_large",
        "body_too_large",
        "invalid_vtt",
    ]
    assert report.failures[3].size == 10 * 1024 * 1024 + 1
    assert report.failures[4].size == len(oversized)
    declared.bytes.assert_not_awaited()
    assert report.saved_paths == ()
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_save_subtitles_sanitizes_network_and_read_exceptions(tmp_path: Path) -> None:
    signed_url = "https://cdn.example.test/sub.vtt?token=signed-secret"
    read_failure = response()
    read_failure.bytes.side_effect = RuntimeError(
        f"body failed for {signed_url}; cookie=private-cookie-value"
    )
    client = MagicMock()
    client.get = AsyncMock(
        side_effect=[RuntimeError(f"GET {signed_url}; Authorization: bearer-secret"), read_failure]
    )
    subtitles = [
        Subtitle(url=signed_url, lang="es"),
        Subtitle(url=signed_url, lang="en"),
    ]
    source = VideoSource(url="https://example.test/video", subtitles=subtitles)

    with patch("evdownloader.service.rnet.Client", return_value=client):
        report = await _save_subtitles(subtitles, tmp_path / "lesson", source)

    assert [failure.reason for failure in report.failures] == ["network_error", "read_error"]
    report_text = repr(report)
    for secret in (signed_url, "signed-secret", "private-cookie-value", "bearer-secret"):
        assert secret not in report_text


@pytest.mark.asyncio
async def test_save_subtitles_reserves_suffix_before_write_failure(tmp_path: Path) -> None:
    subtitles = [
        Subtitle(url="https://example.test/first.vtt", lang="es"),
        Subtitle(url="https://example.test/second.vtt", lang="es"),
    ]
    client = MagicMock()
    client.get = AsyncMock(side_effect=[response(), response(b"WEBVTT\nsecond")])
    source = VideoSource(url="https://example.test/video", subtitles=subtitles)
    real_replace = os.replace
    calls = 0

    def fail_first_replace(source_path: str, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk path contains secret")
        real_replace(source_path, destination)

    with (
        patch("evdownloader.service.rnet.Client", return_value=client),
        patch("evdownloader.service.os.replace", side_effect=fail_first_replace),
    ):
        report = await _save_subtitles(subtitles, tmp_path / "lesson", source)

    assert report.failures[0].reason == "write_error"
    assert report.expected_paths == (
        tmp_path / "lesson.es.vtt",
        tmp_path / "lesson.es-2.vtt",
    )
    assert report.saved_paths == (tmp_path / "lesson.es-2.vtt",)
    assert (tmp_path / "lesson.es-2.vtt").read_text(encoding="utf-8") == "WEBVTT\nsecond"
    assert not list(tmp_path.glob(".lesson.*.tmp"))
    assert "secret" not in repr(report)


@pytest.mark.asyncio
async def test_save_subtitles_atomically_replaces_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external.vtt"
    external.write_text("external-content", encoding="utf-8")
    destination = tmp_path / "lesson.es.vtt"
    destination.symlink_to(external)
    source = VideoSource(
        url="https://example.test/video",
        subtitles=[Subtitle(url="https://example.test/es.vtt", lang="es")],
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=response(b"WEBVTT\nreplacement"))

    with patch("evdownloader.service.rnet.Client", return_value=client):
        report = await _save_subtitles(source.subtitles, tmp_path / "lesson", source)

    assert report.failures == ()
    assert not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == "WEBVTT\nreplacement"
    assert external.read_text(encoding="utf-8") == "external-content"
    assert not list(tmp_path.glob(".lesson.*.tmp"))


@pytest.mark.asyncio
async def test_save_subtitles_publish_failure_preserves_prior_file_and_cleans_temp(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "lesson.es.vtt"
    destination.write_text("WEBVTT\nprior", encoding="utf-8")
    source = VideoSource(
        url="https://example.test/video",
        subtitles=[Subtitle(url="https://example.test/es.vtt", lang="es")],
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=response(b"WEBVTT\nreplacement"))

    with (
        patch("evdownloader.service.rnet.Client", return_value=client),
        patch("evdownloader.service.os.replace", side_effect=OSError("publish failed")),
    ):
        report = await _save_subtitles(source.subtitles, tmp_path / "lesson", source)

    assert report.failures[0].reason == "write_error"
    assert destination.read_text(encoding="utf-8") == "WEBVTT\nprior"
    assert not list(tmp_path.glob(".lesson.*.tmp"))


@pytest.mark.asyncio
async def test_save_subtitles_cancellation_preserves_prior_file_and_cleans_temp(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "lesson.es.vtt"
    destination.write_text("WEBVTT\nprior", encoding="utf-8")
    source = VideoSource(
        url="https://example.test/video",
        subtitles=[Subtitle(url="https://example.test/es.vtt", lang="es")],
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=response(b"WEBVTT\nreplacement"))

    with (
        patch("evdownloader.service.rnet.Client", return_value=client),
        patch("evdownloader.service.os.replace", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await _save_subtitles(source.subtitles, tmp_path / "lesson", source)

    assert destination.read_text(encoding="utf-8") == "WEBVTT\nprior"
    assert not list(tmp_path.glob(".lesson.*.tmp"))


@pytest.mark.asyncio
async def test_save_subtitles_reserves_duplicate_languages_in_input_order(
    tmp_path: Path,
) -> None:
    subtitles = [
        Subtitle(url="https://example.test/network.vtt", lang="es"),
        Subtitle(url="https://example.test/html.vtt", lang="es"),
        Subtitle(url="https://example.test/one.vtt", lang="es"),
        Subtitle(url="https://example.test/two.vtt", lang="es"),
    ]
    client = MagicMock()
    client.get = AsyncMock(
        side_effect=[
            ConnectionError("failed subtitle"),
            response(b"<html>not subtitles</html>"),
            response(b"WEBVTT\none"),
            response(b"WEBVTT\ntwo"),
        ]
    )
    source = VideoSource(url="https://example.test/video", subtitles=subtitles)
    base = tmp_path / "lesson"

    with patch("evdownloader.service.rnet.Client", return_value=client):
        report = await _save_subtitles(subtitles, base, source)

    expected = {
        "lesson.es-3.vtt": "WEBVTT\none",
        "lesson.es-4.vtt": "WEBVTT\ntwo",
    }
    assert {path.name for path in tmp_path.iterdir()} == expected.keys()
    for filename, content in expected.items():
        assert (tmp_path / filename).read_text(encoding="utf-8") == content
    assert report.attempted == 4
    assert [path.name for path in report.expected_paths] == [
        "lesson.es.vtt",
        "lesson.es-2.vtt",
        "lesson.es-3.vtt",
        "lesson.es-4.vtt",
    ]
    assert report.saved_count == 2
    assert [failure.track_index for failure in report.failures] == [0, 1]
