from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evdownloader.models import Cookie
from evdownloader.resource_download import MAX_REDIRECTS, download_resource


class Response:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"ok",
        *,
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        self.body = body
        self.error = error
        self.close = AsyncMock()

    async def stream(self):
        if self.error:
            raise self.error
        if self.body:
            yield self.body


async def run_download(
    tmp_path: Path,
    responses: list[Response] | Exception,
    *,
    url: str = "https://files.example.test/start.bin",
    cookies: list[Cookie] | None = None,
    max_bytes: int = 100,
):
    client = MagicMock()
    if isinstance(responses, Exception):
        client.get = AsyncMock(side_effect=responses)
    else:
        client.get = AsyncMock(side_effect=responses)
    destination = tmp_path / "result.bin"
    with patch("evdownloader.resource_download.rnet.Client", return_value=client) as factory:
        result = await download_resource(
            url,
            destination,
            cookie_jar=cookies or [],
            trusted_host_suffixes=("example.test",),
            max_bytes=max_bytes,
        )
    return result, destination, client, factory


@pytest.mark.asyncio
async def test_cookie_matrix_is_recomputed_for_each_redirect(tmp_path: Path) -> None:
    cookies = [
        Cookie(name="root", value="1", domain=".example.test", path="/"),
        Cookie(name="path", value="2", domain="files.example.test", path="/private"),
        Cookie(name="secure", value="3", domain=".example.test", secure=True),
        Cookie(name="expired", value="4", domain=".example.test", expires=1),
        Cookie(name="other", value="5", domain="other.test"),
    ]
    responses = [
        Response(302, headers={"location": "https://cdn.example.test/private/file.bin"}),
        Response(body=b"content"),
    ]
    result, destination, client, _ = await run_download(tmp_path, responses, cookies=cookies)

    assert result.ok and destination.read_bytes() == b"content"
    first, second = client.get.await_args_list
    assert first.kwargs == {
        "headers": {"Cookie": "root=1; secure=3"},
        "allow_redirects": False,
    }
    assert second.kwargs == {
        "headers": {"Cookie": "root=1; secure=3"},
        "allow_redirects": False,
    }
    assert all(response.close.await_count == 1 for response in responses)


@pytest.mark.asyncio
async def test_untrusted_initial_host_is_rejected_before_request(tmp_path: Path) -> None:
    result, _, client, _ = await run_download(
        tmp_path, [], url="https://example.test.evil.test/signed?token=secret"
    )
    assert result.reason == "untrusted_host"
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_untrusted_redirect_is_rejected_before_second_request(tmp_path: Path) -> None:
    result, _, client, _ = await run_download(
        tmp_path, [Response(302, headers={"location": "https://evil.test/x?token=secret"})]
    )
    assert result.reason == "untrusted_host"
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_relative_redirect_is_supported(tmp_path: Path) -> None:
    result, destination, client, _ = await run_download(
        tmp_path, [Response(302, headers={"location": "/next"}), Response(body=b"next")]
    )
    assert result.ok and destination.read_bytes() == b"next"
    assert client.get.await_args_list[1].args[0] == "https://files.example.test/next"


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded(tmp_path: Path) -> None:
    response = Response(302, headers={"location": "/start.bin"})
    result, _, client, _ = await run_download(tmp_path, [response])
    assert result.reason == "redirect_loop"
    assert client.get.await_count == 1
    response.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_redirect_hop_limit_is_bounded(tmp_path: Path) -> None:
    responses = [Response(302, headers={"location": f"/{index + 1}"}) for index in range(6)]
    result, _, client, _ = await run_download(tmp_path, responses)
    assert result.reason == "redirect_limit"
    assert client.get.await_count == MAX_REDIRECTS + 1
    assert all(response.close.await_count == 1 for response in responses)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "reason"),
    [
        ([Response(403)], "http_status"),
        (OSError("https://files.example.test/x?token=secret"), "network_error"),
        ([Response(headers={"content-length": "101"})], "declared_too_large"),
        ([Response(body=b"x" * 101)], "body_too_large"),
        ([Response(body=b"")], "empty"),
        ([Response(error=OSError("cookie=secret"))], "write_or_read_error"),
    ],
)
async def test_failures_preserve_final_and_clean_staging(
    tmp_path: Path, responses, reason: str
) -> None:
    final = tmp_path / "result.bin"
    final.write_bytes(b"existing")
    result, _, _, _ = await run_download(tmp_path, responses)
    assert result.reason == reason
    assert final.read_bytes() == b"existing"
    assert list(tmp_path.glob(".result.bin.*.tmp")) == []
    assert "secret" not in repr(result)
    if isinstance(responses, list):
        assert all(response.close.await_count == 1 for response in responses)


@pytest.mark.asyncio
async def test_success_atomically_replaces_existing_file_and_staging(tmp_path: Path) -> None:
    final = tmp_path / "result.bin"
    final.write_bytes(b"old")
    response = Response(body=b"new")
    result, _, _, factory = await run_download(tmp_path, [response])
    assert result.ok and final.read_bytes() == b"new"
    assert list(tmp_path.glob(".result.bin.*.tmp")) == []
    assert factory.call_args.kwargs["timeout"] == 30
    response.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_rnet_status_code_uses_as_int_and_closes_response(tmp_path: Path) -> None:
    status = MagicMock()
    status.as_int.return_value = 200
    status.__int__.side_effect = TypeError("int conversion is unsupported")
    response = Response(body=b"content")
    response.status_code = MagicMock(return_value=status)
    result, destination, _, _ = await run_download(tmp_path, [response])
    assert result.ok and destination.read_bytes() == b"content"
    status.as_int.assert_called_once_with()
    response.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_publication_failure_closes_response_once(tmp_path: Path) -> None:
    response = Response(body=b"new")
    with patch("evdownloader.resource_download.os.replace", side_effect=OSError("publish")):
        result, destination, _, _ = await run_download(tmp_path, [response])
    assert result.reason == "write_or_read_error"
    assert not destination.exists()
    assert list(tmp_path.glob(".result.bin.*.tmp")) == []
    response.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_symlink_destination_is_replaced_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"safe")
    destination = tmp_path / "result.bin"
    destination.symlink_to(target)
    result, _, _, _ = await run_download(tmp_path, [Response(body=b"download")])
    assert result.ok
    assert not destination.is_symlink()
    assert destination.read_bytes() == b"download"
    assert target.read_bytes() == b"safe"
