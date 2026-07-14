"""Tests for DRM downloader integration and the manual drm-proof command.

Covers:
- YtDlpDownloader DRM path: encrypted staging, prove_decrypt_path, error handling.
- Missing drm_device gives actionable error.
- source.drm without use_drm fails explicitly.
- DRM handoff in Udemy sets URL to MPD under use_drm.
- post_license_challenge boundary.
- drm-proof CLI callback boundaries.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from evdownloader.config import Settings
from evdownloader.downloaders.ytdlp import YtDlpDownloader
from evdownloader.drm import ProofResult
from evdownloader.drm.license import LicensePostError, post_license_challenge
from evdownloader.extractors.udemy import UdemyExtractor
from evdownloader.models import DrmInfo, Unit, UnitType, VideoSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KID = "a" * 32
_KEY = "b" * 32


def _make_drm_source(**kwargs: object) -> VideoSource:
    defaults: dict = {
        "url": "https://example.com/video.mpd",
        "drm": DrmInfo(
            scheme="widevine",
            pssh="AAAAV3Bzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAADc=",
            license_url="https://license.example.com",
        ),
    }
    defaults.update(kwargs)  # type: ignore[arg-type]
    return VideoSource(**defaults)  # type: ignore[arg-type]


def _settings(**kwargs: object) -> Settings:
    defaults: dict = {
        "download_dir": Path("/tmp/test-dl"),
        "use_drm": True,
        "drm_device": Path("/tmp/device.wvd"),
    }
    defaults.update(kwargs)  # type: ignore[arg-type]
    return Settings(**defaults)  # type: ignore[arg-type]


def _staged_artifact(ytdlp_cls: MagicMock, extension: str) -> Path:
    template = ytdlp_cls.call_args.args[0]["outtmpl"]
    return Path(template.replace("%(ext)s", extension))


# ---------------------------------------------------------------------------
# YtDlpDownloader DRM path
# ---------------------------------------------------------------------------


class TestYtDlpDownloaderDrm:
    """Tests for the DRM code path in YtDlpDownloader._run."""

    def test_encrypted_artifacts_handles_brackets_in_destination_name(
        self, tmp_path: Path
    ) -> None:
        """Literal destination metacharacters do not break staging discovery."""
        dest = tmp_path / "lesson [intro]"
        staging_id = "staging-123"
        audio = tmp_path / f"{dest.stem}.encrypted.{staging_id}.m4a"
        video = tmp_path / f"{dest.stem}.encrypted.{staging_id}.mp4"
        audio.write_bytes(b"audio")
        video.write_bytes(b"video")

        assert YtDlpDownloader._encrypted_artifacts(dest, staging_id) == [audio, video]

    def test_drm_without_use_drm_raises(self) -> None:
        """source.drm + use_drm=False raises actionable error."""
        source = _make_drm_source()
        settings = _settings(use_drm=False)
        downloader = YtDlpDownloader()

        with pytest.raises(RuntimeError, match="--use-drm is disabled"):
            downloader._run(source, Path("/tmp/out"), settings)

    def test_drm_missing_device_raises(self) -> None:
        """source.drm + use_drm=True + no drm_device raises actionable error."""
        source = _make_drm_source()
        settings = _settings(drm_device=None)
        downloader = YtDlpDownloader()

        with pytest.raises(RuntimeError, match="--drm-device"):
            downloader._run(source, Path("/tmp/out"), settings)

    def test_drm_device_not_found_raises(self, tmp_path: Path) -> None:
        """source.drm + use_drm=True + non-existent device raises."""
        source = _make_drm_source()
        settings = _settings(drm_device=tmp_path / "missing.wvd")
        downloader = YtDlpDownloader()

        with pytest.raises(RuntimeError, match="Device file not found"):
            downloader._run(source, Path("/tmp/out"), settings)

    def test_drm_download_calls_prove_decrypt_path(self, tmp_path: Path) -> None:
        """DRM path downloads encrypted, then calls prove_decrypt_path."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake-device")
        source = _make_drm_source()
        settings = _settings(
            download_dir=tmp_path,
            drm_device=device,
        )
        downloader = YtDlpDownloader()

        proof_result = ProofResult(output_path=tmp_path / "001.mp4", keys=[])

        with (
            patch("yt_dlp.YoutubeDL") as mock_ydl_cls,
            patch(
                "evdownloader.drm.prove_decrypt_path",
                new_callable=AsyncMock,
            ) as mock_proof,
        ):
            mock_ydl_instance = MagicMock()
            mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl_instance)
            mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl_instance.download = MagicMock()

            # Make encrypted_staging exist after download.
            def fake_download(urls):
                _staged_artifact(mock_ydl_cls, "mp4").write_bytes(b"encrypted-data")

            mock_ydl_instance.download.side_effect = fake_download

            mock_proof.return_value = proof_result

            result = downloader._run_drm(source, tmp_path / "001", settings)

            assert result == proof_result.output_path
            mock_proof.assert_called_once()

    def test_drm_removes_encrypted_on_success(self, tmp_path: Path) -> None:
        """Encrypted staging file is removed after successful decrypt."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake-device")
        source = _make_drm_source()
        settings = _settings(download_dir=tmp_path, drm_device=device)
        downloader = YtDlpDownloader()

        proof_result = ProofResult(output_path=tmp_path / "001.mp4", keys=[])

        with (
            patch("yt_dlp.YoutubeDL") as mock_ydl_cls,
            patch(
                "evdownloader.drm.prove_decrypt_path",
                new_callable=AsyncMock,
            ) as mock_proof,
        ):
            mock_ydl_instance = MagicMock()
            mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl_instance)
            mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

            def fake_download(urls):
                _staged_artifact(mock_ydl_cls, "mp4").write_bytes(b"encrypted-data")

            mock_ydl_instance.download.side_effect = fake_download
            mock_proof.return_value = proof_result

            downloader._run_drm(source, tmp_path / "001", settings)

            assert not _staged_artifact(mock_ydl_cls, "mp4").exists()

    def test_drm_keeps_encrypted_on_failure(self, tmp_path: Path) -> None:
        """Encrypted staging file is kept when decryption fails."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake-device")
        source = _make_drm_source()
        settings = _settings(download_dir=tmp_path, drm_device=device)
        downloader = YtDlpDownloader()

        with (
            patch("yt_dlp.YoutubeDL") as mock_ydl_cls,
            patch(
                "evdownloader.drm.prove_decrypt_path",
                new_callable=AsyncMock,
            ) as mock_proof,
        ):
            mock_ydl_instance = MagicMock()
            mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl_instance)
            mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

            def fake_download(urls):
                _staged_artifact(mock_ydl_cls, "mp4").write_bytes(b"encrypted-data")

            mock_ydl_instance.download.side_effect = fake_download
            mock_proof.side_effect = Exception("decrypt failed")

            with pytest.raises(RuntimeError, match="DRM decryption failed"):
                downloader._run_drm(source, tmp_path / "001", settings)

            assert _staged_artifact(mock_ydl_cls, "mp4").exists()

    def test_drm_refreshes_token_after_download(self, tmp_path: Path) -> None:
        """The provider token is refreshed after download and before the proof call."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake-device")
        events: list[str] = []
        source = _make_drm_source()
        refreshed = source.drm.model_copy(update={"token": "fresh-provider-token"})

        async def refresh():
            events.append("refresh")
            return refreshed

        source.drm_refresher = refresh
        settings = _settings(download_dir=tmp_path, drm_device=device)
        proof_result = ProofResult(output_path=tmp_path / "001.mp4", keys=[])

        async def fake_proof(**kwargs):
            events.append("proof")
            assert kwargs["license_input"].token == "fresh-provider-token"
            return proof_result

        with (
            patch("yt_dlp.YoutubeDL") as mock_ydl_cls,
            patch("evdownloader.drm.prove_decrypt_path", new_callable=AsyncMock) as mock_proof,
        ):
            instance = MagicMock()
            mock_ydl_cls.return_value.__enter__.return_value = instance
            mock_ydl_cls.return_value.__exit__.return_value = False

            def fake_download(_urls) -> None:
                events.append("download")
                _staged_artifact(mock_ydl_cls, "mp4").write_bytes(b"encrypted")

            instance.download.side_effect = fake_download
            mock_proof.side_effect = fake_proof

            result = YtDlpDownloader()._run_drm(source, tmp_path / "001", settings)

        assert result == proof_result.output_path
        assert events == ["download", "refresh", "proof"]

    def test_drm_explicit_token_skips_refresh_and_wins(self, tmp_path: Path) -> None:
        """An explicit CLI token avoids refresh and remains the highest priority."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake-device")
        source = _make_drm_source()
        refresh = AsyncMock(side_effect=AssertionError("refresh must not run"))
        source.drm_refresher = refresh
        settings = _settings(download_dir=tmp_path, drm_device=device, drm_token="explicit-token")
        proof_result = ProofResult(output_path=tmp_path / "001.mp4", keys=[])

        with (
            patch("yt_dlp.YoutubeDL") as mock_ydl_cls,
            patch("evdownloader.drm.prove_decrypt_path", new_callable=AsyncMock) as mock_proof,
        ):
            instance = MagicMock()
            mock_ydl_cls.return_value.__enter__.return_value = instance
            mock_ydl_cls.return_value.__exit__.return_value = False
            instance.download.side_effect = lambda _urls: _staged_artifact(
                mock_ydl_cls, "mp4"
            ).write_bytes(b"encrypted")
            mock_proof.return_value = proof_result

            YtDlpDownloader()._run_drm(source, tmp_path / "001", settings)

        refresh.assert_not_awaited()
        assert mock_proof.call_args.kwargs["license_input"].token == "explicit-token"

    def test_drm_processes_all_compatible_artifacts_in_order(self, tmp_path: Path) -> None:
        """Separate video/audio artifacts are passed to the proof pipeline in order."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake-device")
        source = _make_drm_source()
        settings = _settings(download_dir=tmp_path, drm_device=device)
        proof_result = ProofResult(output_path=tmp_path / "001.mp4", keys=[])

        with (
            patch("yt_dlp.YoutubeDL") as mock_ydl_cls,
            patch("evdownloader.drm.prove_decrypt_path", new_callable=AsyncMock) as mock_proof,
        ):
            instance = MagicMock()
            mock_ydl_cls.return_value.__enter__.return_value = instance
            mock_ydl_cls.return_value.__exit__.return_value = False

            def fake_download(_urls) -> None:
                _staged_artifact(mock_ydl_cls, "mp4").write_bytes(b"video")
                _staged_artifact(mock_ydl_cls, "m4a").write_bytes(b"audio")

            instance.download.side_effect = fake_download
            mock_proof.return_value = proof_result

            YtDlpDownloader()._run_drm(source, tmp_path / "001", settings)

        assert [path.suffix for path in mock_proof.call_args.kwargs["encrypted_path"]] == [
            ".m4a",
            ".mp4",
        ]

    def test_drm_without_artifact_fails_clearly(self, tmp_path: Path) -> None:
        """A successful yt-dlp call without compatible output is rejected."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake-device")
        source = _make_drm_source()
        settings = _settings(download_dir=tmp_path, drm_device=device)

        with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
            instance = MagicMock()
            mock_ydl_cls.return_value.__enter__.return_value = instance
            mock_ydl_cls.return_value.__exit__.return_value = False

            with pytest.raises(RuntimeError, match="no compatible encrypted file"):
                YtDlpDownloader()._run_drm(source, tmp_path / "001", settings)

    def test_drm_retries_use_isolated_staging(self, tmp_path: Path) -> None:
        """A retry only consumes and removes its own encrypted artifacts."""
        from uuid import UUID

        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake-device")
        source = _make_drm_source()
        settings = _settings(download_dir=tmp_path, drm_device=device)
        proof_result = ProofResult(output_path=tmp_path / "001.mp4", keys=[])
        ytdlp_instance = MagicMock()
        attempts = iter(["first", "second"])

        def fake_download(_urls) -> None:
            label = next(attempts)
            _staged_artifact(mock_ydl_cls, "mp4").write_bytes(label.encode())

        with (
            patch(
                "evdownloader.downloaders.ytdlp.uuid.uuid4",
                side_effect=[
                    UUID("00000000-0000-0000-0000-000000000001"),
                    UUID("00000000-0000-0000-0000-000000000002"),
                ],
            ),
            patch("yt_dlp.YoutubeDL") as mock_ydl_cls,
            patch("evdownloader.drm.prove_decrypt_path", new_callable=AsyncMock) as mock_proof,
        ):
            mock_ydl_cls.return_value.__enter__.return_value = ytdlp_instance
            mock_ydl_cls.return_value.__exit__.return_value = False
            ytdlp_instance.download.side_effect = fake_download
            mock_proof.side_effect = [RuntimeError("first attempt"), proof_result]

            with pytest.raises(RuntimeError, match="DRM decryption failed"):
                YtDlpDownloader()._run_drm(source, tmp_path / "001", settings)
            first = tmp_path / "001.encrypted.00000000000000000000000000000001.mp4"
            assert first.exists()

            YtDlpDownloader()._run_drm(source, tmp_path / "001", settings)
            second = tmp_path / "001.encrypted.00000000000000000000000000000002.mp4"
            assert not second.exists()
            assert first.exists()
            assert mock_proof.call_args.kwargs["encrypted_path"] == [second]

    def test_drm_refresh_without_token_is_explicit_error(self, tmp_path: Path) -> None:
        """A refresh callback returning no token cannot silently reuse stale metadata."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake-device")
        source = _make_drm_source()
        source.drm_refresher = AsyncMock(return_value=None)
        settings = _settings(download_dir=tmp_path, drm_device=device)

        with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
            instance = MagicMock()
            mock_ydl_cls.return_value.__enter__.return_value = instance
            mock_ydl_cls.return_value.__exit__.return_value = False
            instance.download.side_effect = lambda _urls: _staged_artifact(
                mock_ydl_cls, "mp4"
            ).write_bytes(b"encrypted")

            with pytest.raises(RuntimeError, match="refresh returned no token"):
                YtDlpDownloader()._run_drm(source, tmp_path / "001", settings)


# ---------------------------------------------------------------------------
# Udemy DRM handoff
# ---------------------------------------------------------------------------


class TestUdemyDrmHandoff:
    """Tests for DRM source handoff in Udemy._attach_drm."""

    def test_drm_mode_sets_url_to_mpd(self) -> None:
        """Under use_drm, source.url is the MPD, is_embed=False, write_subs=False."""
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

        from tests.test_udemy import _drm_asset, _make_jwt

        async def fake_fetch_drm_asset(course_id: str, lecture_id: str) -> dict:
            return _drm_asset(_make_jwt(time.time() + 3600))

        async def fake_fetch_text(url: str) -> str:
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
        assert src.url == "https://dash-enc-cdn77.udemycdn.com/cmaf/asset/cenc/stream.mpd"
        assert src.is_embed is False
        assert src.write_subs is False
        assert src.drm is not None

    def test_non_drm_mode_preserves_defaults(self) -> None:
        """Without use_drm, URL stays as lecture page, is_embed=True, write_subs=True."""
        ex = UdemyExtractor()
        # No configure / use_drm defaults to False.
        unit = Unit(
            title="x",
            url="https://www.udemy.com/x/lecture/1",
            type=UnitType.VIDEO,
            index=1,
        )
        src = asyncio.run(ex.resolve_video(None, unit))

        assert src is not None
        assert src.url == "https://www.udemy.com/x/lecture/1"
        assert src.is_embed is True
        assert src.write_subs is True
        assert src.drm is None


# ---------------------------------------------------------------------------
# post_license_challenge
# ---------------------------------------------------------------------------


class _FakeStatusCode:
    """Minimal StatusCode mock with is_success() and as_int()."""

    def __init__(self, code: int) -> None:
        self._code = code

    def is_success(self) -> bool:
        return 200 <= self._code < 300

    def as_int(self) -> int:
        return self._code

    def __str__(self) -> str:
        return str(self._code)


class TestPostLicenseChallenge:
    """Tests for the license POST boundary."""

    def test_posts_raw_bytes_with_octet_stream(self) -> None:
        """Challenge is sent as application/octet-stream by default."""
        with patch("rnet.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_resp = MagicMock()
            mock_resp.status_code = _FakeStatusCode(200)
            mock_resp.bytes = AsyncMock(return_value=b"license-response")
            mock_client.post = AsyncMock(return_value=mock_resp)

            result = asyncio.run(
                post_license_challenge(
                    "https://license.example.com",
                    b"challenge-data",
                    {},
                )
            )

            assert result == b"license-response"
            call_kwargs = mock_client.post.call_args
            assert call_kwargs[1]["body"] == b"challenge-data"
            assert call_kwargs[1]["headers"]["Content-Type"] == "application/octet-stream"

    def test_preserves_existing_content_type(self) -> None:
        """If caller already set Content-Type, it is not overridden."""
        with patch("rnet.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_resp = MagicMock()
            mock_resp.status_code = _FakeStatusCode(200)
            mock_resp.bytes = AsyncMock(return_value=b"response")
            mock_client.post = AsyncMock(return_value=mock_resp)

            result = asyncio.run(
                post_license_challenge(
                    "https://license.example.com",
                    b"challenge",
                    {"Content-Type": "custom/type"},
                )
            )

            assert result == b"response"
            call_kwargs = mock_client.post.call_args
            assert call_kwargs[1]["headers"]["Content-Type"] == "custom/type"

    def test_non_2xx_raises_license_post_error(self) -> None:
        """Non-2xx status raises LicensePostError without response body."""
        with patch("rnet.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_resp = MagicMock()
            mock_resp.status_code = _FakeStatusCode(403)
            mock_resp.bytes = AsyncMock(return_value=b"forbidden body with secret token")
            mock_client.post = AsyncMock(return_value=mock_resp)

            with pytest.raises(LicensePostError, match="HTTP 403") as exc_info:
                asyncio.run(
                    post_license_challenge(
                        "https://license.example.com",
                        b"challenge",
                        {},
                    )
                )

            # Response body must NOT leak into error.
            assert "secret token" not in str(exc_info.value)

    def test_network_error_raises_license_post_error(self) -> None:
        """Network errors raise LicensePostError without challenge body."""
        with patch("rnet.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.post = AsyncMock(side_effect=ConnectionError("timeout"))

            with pytest.raises(LicensePostError, match="network error") as exc_info:
                asyncio.run(
                    post_license_challenge(
                        "https://license.example.com",
                        b"secret-challenge",
                        {},
                    )
                )

            assert "secret-challenge" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# CLI drm-proof parse/header helper
# ---------------------------------------------------------------------------


class TestDrmProofCli:
    """Tests for the drm-proof CLI command boundaries."""

    def test_cli_app_has_drm_proof_command(self) -> None:
        """The drm_proof callback is importable and is a typer command."""
        from evdownloader.cli import drm_proof

        # Typer wraps the function; it should still be callable.
        assert callable(drm_proof)

    def test_drm_proof_callback_rejects_invalid_header(self) -> None:
        """A header without ':' is rejected."""
        from evdownloader.cli import drm_proof

        with pytest.raises(typer.Exit) as exc_info:
            drm_proof(
                input=Path("/tmp/enc.mp4"),
                output=Path("/tmp/out.mp4"),
                device=Path("/tmp/device.wvd"),
                license_url="https://license.example.com",
                pssh="AAAA",
                token=None,
                key_id=None,
                header=["bad-header-no-colon"],
            )
        assert exc_info.value.exit_code == 1

    def test_drm_proof_callback_rejects_missing_input(self) -> None:
        """A non-existent input file is rejected."""
        from evdownloader.cli import drm_proof

        with pytest.raises(typer.Exit) as exc_info:
            drm_proof(
                input=Path("/tmp/nonexistent-enc.mp4"),
                output=Path("/tmp/out.mp4"),
                device=Path("/tmp/nonexistent-device.wvd"),
                license_url="https://license.example.com",
                pssh="AAAA",
                token=None,
                key_id=None,
                header=[],
            )
        assert exc_info.value.exit_code == 1
