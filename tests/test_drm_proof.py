"""Tests for DRM decrypt path — cdm, decrypt, and proof boundaries.

All network, CDM, and mp4decrypt behaviour is mocked so no real files,
keys, or licenses are involved.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evdownloader.drm.cdm import CdmUnavailableError, WidevineCdmSession
from evdownloader.drm.decrypt import (
    ContentKey,
    DecryptError,
    build_mp4decrypt_command,
    run_mp4decrypt,
)
from evdownloader.drm.license import WidevineLicenseInput
from evdownloader.drm.proof import ProofError, prove_decrypt_path

_KID1 = "a" * 32
_KEY1 = "b" * 32
_KID2 = "c" * 32
_KEY2 = "d" * 32

# ============================================================================
# ContentKey tests
# ============================================================================


class TestContentKey:
    """Tests for the ContentKey dataclass."""

    def test_valid_hex(self) -> None:
        ck = ContentKey(kid=_KID1, key=_KEY1)
        assert ck.kid == _KID1
        assert ck.key == _KEY1

    def test_empty_kid_raises(self) -> None:
        with pytest.raises(ValueError, match="KID must be a 16-byte hex"):
            ContentKey(kid="", key=_KEY1)

    def test_empty_key_raises(self) -> None:
        with pytest.raises(ValueError, match="KEY must be a 16-byte hex"):
            ContentKey(kid=_KID1, key="")

    def test_non_hex_kid_raises(self) -> None:
        with pytest.raises(ValueError, match="KID must be a 16-byte hex"):
            ContentKey(kid="z" * 32, key=_KEY1)

    def test_non_hex_key_raises(self) -> None:
        with pytest.raises(ValueError, match="KEY must be a 16-byte hex"):
            ContentKey(kid=_KID1, key="z" * 32)


# ============================================================================
# build_mp4decrypt_command tests
# ============================================================================


class TestBuildMp4decryptCommand:
    """Tests for mp4decrypt command construction."""

    def test_single_key(self) -> None:
        keys = [ContentKey(kid=_KID1, key=_KEY1)]
        cmd = build_mp4decrypt_command("/in.mp4", "/out.mp4", keys)
        assert cmd == [
            "mp4decrypt",
            "--key", f"{_KID1}:{_KEY1}",
            "/in.mp4",
            "/out.mp4",
        ]

    def test_multiple_keys_order_preserved(self) -> None:
        keys = [
            ContentKey(kid=_KID1, key=_KEY1),
            ContentKey(kid=_KID2, key=_KEY2),
        ]
        cmd = build_mp4decrypt_command("/in.mp4", "/out.mp4", keys)
        assert "--key" in cmd
        assert f"{_KID1}:{_KEY1}" in cmd
        assert f"{_KID2}:{_KEY2}" in cmd
        # First key appears before second
        idx1 = cmd.index(f"{_KID1}:{_KEY1}")
        idx2 = cmd.index(f"{_KID2}:{_KEY2}")
        assert idx1 < idx2

    def test_no_keys_raises_decrypt_error(self) -> None:
        with pytest.raises(DecryptError, match="At least one content key"):
            build_mp4decrypt_command("/in.mp4", "/out.mp4", [])

    def test_overwrite_flag(self) -> None:
        keys = [ContentKey(kid=_KID1, key=_KEY1)]
        cmd = build_mp4decrypt_command("/in", "/out", keys, overwrite=True)
        assert "--force-overwrite" in cmd
        # Overwrite flag must come before positional args
        assert cmd.index("--force-overwrite") < cmd.index("/in")

    def test_path_types_accepted(self) -> None:
        keys = [ContentKey(kid=_KID1, key=_KEY1)]
        cmd = build_mp4decrypt_command(Path("/a.mp4"), Path("/b.mp4"), keys)
        assert "/a.mp4" in cmd
        assert "/b.mp4" in cmd

    def test_keys_not_mutated_after_build(self) -> None:
        keys = [ContentKey(kid=_KID1, key=_KEY1)]
        _ = build_mp4decrypt_command("/in", "/out", keys)
        assert keys[0].kid == _KID1
        assert keys[0].key == _KEY1


# ============================================================================
# run_mp4decrypt tests
# ============================================================================


class TestRunMp4decrypt:
    """Tests for the async mp4decrypt runner."""

    @pytest.mark.asyncio
    async def test_success_renames_temp(self, tmp_path: Path) -> None:
        """On success, temporary file is renamed to final output."""
        keys = [ContentKey(kid=_KID1, key=_KEY1)]
        input_file = tmp_path / "enc.mp4"
        output_file = tmp_path / "dec.mp4"
        input_file.write_bytes(b"encrypted")

        tmp_file = output_file.with_suffix(output_file.suffix + ".mp4decrypt.tmp")

        mock_proc = AsyncMock()

        async def _fake_communicate() -> tuple[bytes, bytes]:
            tmp_file.write_bytes(b"decrypted")
            return b"", b""

        mock_proc.communicate = _fake_communicate
        mock_proc.returncode = 0

        with patch("evdownloader.drm.decrypt.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = mock_proc

            result = await run_mp4decrypt(input_file, output_file, keys)

            assert result == output_file
            # Temp file should not exist after rename
            assert not tmp_file.exists()

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises(self, tmp_path: Path) -> None:
        """Non-zero exit code raises DecryptError without key exposure."""
        keys = [ContentKey(kid=_KID1, key=_KEY1)]
        input_file = tmp_path / "enc.mp4"
        output_file = tmp_path / "dec.mp4"
        input_file.write_bytes(b"encrypted")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_proc.returncode = 1

        with patch("evdownloader.drm.decrypt.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = mock_proc

            with pytest.raises(DecryptError, match="exited with code 1") as exc_info:
                await run_mp4decrypt(input_file, output_file, keys)

            # Key secret must NOT appear in error; KID is not sensitive and may appear
            err_msg = str(exc_info.value)
            assert _KEY1 not in err_msg

    @pytest.mark.asyncio
    async def test_partial_file_cleaned_on_failure(self, tmp_path: Path) -> None:
        """Partial temp file is removed when mp4decrypt fails."""
        keys = [ContentKey(kid=_KID1, key=_KEY1)]
        input_file = tmp_path / "enc.mp4"
        output_file = tmp_path / "dec.mp4"
        input_file.write_bytes(b"encrypted")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"err"))
        mock_proc.returncode = 1

        with patch("evdownloader.drm.decrypt.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = mock_proc

            with pytest.raises(DecryptError):
                await run_mp4decrypt(input_file, output_file, keys)

            # Temp file should not survive
            tmp_file = output_file.with_suffix(output_file.suffix + ".mp4decrypt.tmp")
            assert not tmp_file.exists()

    @pytest.mark.asyncio
    async def test_file_not_found_error(self, tmp_path: Path) -> None:
        """Missing mp4decrypt binary produces actionable error."""
        keys = [ContentKey(kid=_KID1, key=_KEY1)]
        input_file = tmp_path / "enc.mp4"
        output_file = tmp_path / "dec.mp4"
        input_file.write_bytes(b"")

        with patch("evdownloader.drm.decrypt.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.side_effect = FileNotFoundError("mp4decrypt not found")

            with pytest.raises(DecryptError, match="mp4decrypt not found"):
                await run_mp4decrypt(input_file, output_file, keys)

    @pytest.mark.asyncio
    async def test_no_keys_raises_before_subprocess(self, tmp_path: Path) -> None:
        """Empty keys raises DecryptError without spawning a subprocess."""
        with pytest.raises(DecryptError, match="At least one content key"):
            await run_mp4decrypt("/nonexistent", "/nonexistent", [])

    @pytest.mark.asyncio
    async def test_existing_output_requires_overwrite(self, tmp_path: Path) -> None:
        """Existing final output is not replaced unless overwrite=True."""
        input_file = tmp_path / "enc.mp4"
        output_file = tmp_path / "dec.mp4"
        input_file.write_bytes(b"encrypted")
        output_file.write_bytes(b"existing")
        keys = [ContentKey(kid=_KID1, key=_KEY1)]

        with pytest.raises(DecryptError, match="already exists"):
            await run_mp4decrypt(input_file, output_file, keys)


# ============================================================================
# CdmUnavailableError simulation tests
# ============================================================================


class TestCdmUnavailableSimulation:
    """Simulate CdmUnavailableError by monkeypatching imports."""

    def test_cdm_unavailable_when_pywidevine_missing(self) -> None:
        """WidevineCdmSession.open raises when pywidevine is not importable."""
        device_path = Path(__file__)
        session = WidevineCdmSession(device_path=device_path)
        with (
            patch.dict("sys.modules", {"pywidevine": None}),
            pytest.raises(CdmUnavailableError, match="pywidevine is not installed"),
        ):
            session.open()

    def test_cdm_device_not_found(self, tmp_path: Path) -> None:
        """WidevineCdmSession.open raises when device file does not exist."""
        session = WidevineCdmSession(device_path=tmp_path / "missing.wvd")
        fake_pww = MagicMock()
        with (
            patch.dict("sys.modules", {"pywidevine": fake_pww}),
            pytest.raises(CdmUnavailableError, match="Device file not found"),
        ):
            session.open()


# ============================================================================
# proof helper tests
# ============================================================================


def _make_license_input(
    pssh: str = "AAAA", url: str = "https://lic.example.com"
) -> WidevineLicenseInput:
    return WidevineLicenseInput(license_url=url, pssh=pssh)


class FakeCdmSession:
    """Minimal fake CDM session for proof tests."""

    def __init__(self, *, device_path: Path) -> None:
        self.device_path = device_path

    def open(self) -> FakeCdmSession:
        return self

    def generate_challenge(self, pssh: str) -> bytes:
        return b"fake-challenge"

    def parse_license_response(self, response: bytes) -> list[tuple[str, str]]:
        return [(_KID1, _KEY1)]


async def _fake_license_post(
    url: str, challenge: bytes, headers: dict[str, str]
) -> bytes:
    return b"fake-license-response"


class TestProofHelper:
    """Tests for the proof helper composition."""

    @pytest.mark.asyncio
    async def test_full_pipeline_mocked(self, tmp_path: Path) -> None:
        """Proof composes fake CDM + license post + mp4decrypt successfully."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake-device")
        enc = tmp_path / "enc.mp4"
        enc.write_bytes(b"encrypted-data")
        out = tmp_path / "dec.mp4"

        license_input = _make_license_input()

        with patch("evdownloader.drm.proof.run_mp4decrypt", new_callable=AsyncMock) as mock_decrypt:
            mock_decrypt.return_value = out
            result = await prove_decrypt_path(
                license_input=license_input,
                device_path=device,
                encrypted_path=enc,
                output_path=out,
                license_post=_fake_license_post,
                cdm_session_cls=FakeCdmSession,  # type: ignore[arg-type]
            )

            assert result.output_path == out
            assert len(result.keys) == 1
            assert result.keys[0].kid == _KID1
            mock_decrypt.assert_called_once()

    @pytest.mark.asyncio
    async def test_udemy_license_input_posts_to_runtime_proxy_url(self, tmp_path: Path) -> None:
        """Udemy proxy URL is built with auth_token only for the runtime POST."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake-device")
        enc = tmp_path / "enc.mp4"
        enc.write_bytes(b"encrypted-data")
        out = tmp_path / "dec.mp4"
        license_input = WidevineLicenseInput(
            license_url="https://www.udemy.com/media-license-server/validate-auth-token",
            pssh="AAAA",
            token="secret-jwt",
        )
        seen_url = ""

        async def post(url: str, challenge: bytes, headers: dict[str, str]) -> bytes:
            nonlocal seen_url
            seen_url = url
            return b"fake-license-response"

        with patch("evdownloader.drm.proof.run_mp4decrypt", new_callable=AsyncMock) as mock_decrypt:
            mock_decrypt.return_value = out
            await prove_decrypt_path(
                license_input=license_input,
                device_path=device,
                encrypted_path=enc,
                output_path=out,
                license_post=post,
                cdm_session_cls=FakeCdmSession,  # type: ignore[arg-type]
            )

        assert seen_url == (
            "https://www.udemy.com/media-license-server/validate-auth-token"
            "?drm_type=widevine&auth_token=secret-jwt"
        )

    @pytest.mark.asyncio
    async def test_missing_device_file(self, tmp_path: Path) -> None:
        """Proof raises ProofError when device file is missing."""
        out = tmp_path / "dec.mp4"
        license_input = _make_license_input()

        with pytest.raises(ProofError, match="Device file not found"):
            await prove_decrypt_path(
                license_input=license_input,
                device_path=tmp_path / "missing.wvd",
                encrypted_path=tmp_path / "enc.mp4",
                output_path=out,
                license_post=_fake_license_post,
                cdm_session_cls=FakeCdmSession,  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_missing_encrypted_file(self, tmp_path: Path) -> None:
        """Proof raises ProofError when encrypted file is missing."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake")
        out = tmp_path / "dec.mp4"
        license_input = _make_license_input()

        with pytest.raises(ProofError, match="Encrypted file not found"):
            await prove_decrypt_path(
                license_input=license_input,
                device_path=device,
                encrypted_path=tmp_path / "missing.mp4",
                output_path=out,
                license_post=_fake_license_post,
                cdm_session_cls=FakeCdmSession,  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_empty_license_response(self, tmp_path: Path) -> None:
        """Proof raises ProofError when license response has no keys."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake")
        enc = tmp_path / "enc.mp4"
        enc.write_bytes(b"data")
        out = tmp_path / "dec.mp4"
        license_input = _make_license_input()

        async def empty_post(url: str, ch: bytes, h: dict[str, str]) -> bytes:
            return b""

        with pytest.raises(ProofError, match="empty response"):
            await prove_decrypt_path(
                license_input=license_input,
                device_path=device,
                encrypted_path=enc,
                output_path=out,
                license_post=empty_post,
                cdm_session_cls=FakeCdmSession,  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_license_post_failure(self, tmp_path: Path) -> None:
        """Proof raises ProofError when license POST raises."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake")
        enc = tmp_path / "enc.mp4"
        enc.write_bytes(b"data")
        out = tmp_path / "dec.mp4"
        license_input = _make_license_input()

        async def failing_post(url: str, ch: bytes, h: dict[str, str]) -> bytes:
            raise ConnectionError("network down")

        with pytest.raises(ProofError, match="License POST failed"):
            await prove_decrypt_path(
                license_input=license_input,
                device_path=device,
                encrypted_path=enc,
                output_path=out,
                license_post=failing_post,
                cdm_session_cls=FakeCdmSession,  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_missing_pssh_raises(self, tmp_path: Path) -> None:
        """Proof raises ProofError when PSSH is empty."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake")
        enc = tmp_path / "enc.mp4"
        enc.write_bytes(b"data")
        out = tmp_path / "dec.mp4"

        license_input = WidevineLicenseInput(
            license_url="https://lic.example.com", pssh=""
        )

        with pytest.raises(ProofError, match="PSSH is required"):
            await prove_decrypt_path(
                license_input=license_input,
                device_path=device,
                encrypted_path=enc,
                output_path=out,
                license_post=_fake_license_post,
                cdm_session_cls=FakeCdmSession,  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_no_keys_in_error_on_decrypt_failure(self, tmp_path: Path) -> None:
        """DecryptError during mp4decrypt is caught, keys not exposed."""
        device = tmp_path / "device.wvd"
        device.write_bytes(b"fake")
        enc = tmp_path / "enc.mp4"
        enc.write_bytes(b"data")
        out = tmp_path / "dec.mp4"
        license_input = _make_license_input()

        with patch("evdownloader.drm.proof.run_mp4decrypt", new_callable=AsyncMock) as mock_decrypt:
            mock_decrypt.side_effect = DecryptError("mp4decrypt failed")

            with pytest.raises(ProofError, match="Decryption failed"):
                await prove_decrypt_path(
                    license_input=license_input,
                    device_path=device,
                    encrypted_path=enc,
                    output_path=out,
                    license_post=_fake_license_post,
                    cdm_session_cls=FakeCdmSession,  # type: ignore[arg-type]
                )
