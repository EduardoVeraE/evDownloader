"""High-level proof helper — composes CDM, license, and decrypt.

This module wires together:
    * :class:`~evdownloader.drm.license.WidevineLicenseInput`
    * :class:`~evdownloader.drm.cdm.WidevineCdmSession`
    * :class:`~evdownloader.drm.decrypt.ContentKey`
    * :class:`~evdownloader.drm.decrypt.run_mp4decrypt`

It is the single entry-point that proves the full decrypt path.
Network and CDM behaviour are dependency-injected so tests can mock
everything without touching real files, licenses, or device keys.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cdm import CdmUnavailableError, WidevineCdmSession
from .decrypt import (
    ContentKey,
    DecryptError,
    _mp4decrypt_tmp_path,
    probe_stream_types,
    run_ffmpeg_mux,
    run_mp4decrypt,
    validate_mp4_streams,
)
from .license import (
    UDEMY_WIDEVINE_PROXY_URL,
    WidevineLicenseInput,
    build_udemy_widevine_proxy_url,
)


class ProofError(Exception):
    """Raised when the full proof pipeline fails."""


@dataclass(frozen=True, slots=True)
class ProofResult:
    """Outcome of a successful decrypt proof."""

    output_path: Path
    keys: list[ContentKey]


def _cleanup_decryption_outputs(
    output_path: Path, decrypted_paths: Sequence[Path]
) -> None:
    """Remove final, part, and known temporary outputs after validation failure."""
    paths = {
        output_path,
        output_path.with_suffix(output_path.suffix + ".ffmpeg.tmp"),
    }
    for decrypted in decrypted_paths:
        paths.update(
            {
                decrypted,
                _mp4decrypt_tmp_path(decrypted),
            }
        )
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


# Type alias for the injectable license POST function.
LicensePostFn = Callable[
    [str, bytes, dict[str, str]],
    Coroutine[Any, Any, bytes],
]


async def prove_decrypt_path(
    *,
    license_input: WidevineLicenseInput,
    device_path: Path,
    encrypted_path: Path | Sequence[Path],
    output_path: Path,
    license_post: LicensePostFn,
    cdm_session_cls: type[WidevineCdmSession] = WidevineCdmSession,
    validate_output: bool = False,
    overwrite: bool = False,
) -> ProofResult:
    """Run the full decrypt proof pipeline.

    Steps:
        1. Load CDM device and generate a challenge from the PSSH.
        2. POST the challenge to the license server.
        3. Parse content keys from the license response.
        4. Run ``mp4decrypt`` to produce cleartext output.

    Args:
        license_input: Normalized Widevine license inputs.
        device_path: Path to a ``.wvd`` device file.
        encrypted_path: Encrypted ``.mp4`` input file.
        output_path: Desired decrypted output path.
        license_post: Injectable async callable that POSTs the license
            challenge and returns raw license response bytes.
            Signature: ``async def post(url, challenge, headers) -> bytes``
        cdm_session_cls: Class to use for CDM sessions (default:
            :class:`WidevineCdmSession`; override in tests).

    Returns:
        :class:`ProofResult` with the output path and content keys used.

    Raises:
        ProofError: on any step failure with a descriptive message.
        CdmUnavailableError: if pywidevine is missing (propagated).
        LicenseInputError: if the license input is invalid (propagated).
    """
    # -- validate inputs ------------------------------------------------------
    if not license_input.pssh:
        raise ProofError("PSSH is required but was not provided in license_input.")
    if not license_input.license_url:
        raise ProofError("License URL is required but was not provided in license_input.")
    if not device_path.is_file():
        raise ProofError(f"Device file not found: {device_path}")
    encrypted_paths = [encrypted_path] if isinstance(encrypted_path, Path) else list(encrypted_path)
    if not encrypted_paths:
        raise ProofError("At least one encrypted file is required.")
    missing = [path for path in encrypted_paths if not path.is_file()]
    if missing:
        raise ProofError(f"Encrypted file not found: {missing[0]}")

    # -- step 1: CDM challenge ------------------------------------------------
    session = None
    try:
        try:
            session = cdm_session_cls(device_path=device_path).open()
            challenge = session.generate_challenge(license_input.pssh)
        except CdmUnavailableError:
            raise
        except Exception as exc:
            raise ProofError(
                f"Failed to generate Widevine challenge: {type(exc).__name__}"
            ) from exc

        # -- step 2: license POST ---------------------------------------------
        try:
            response_bytes = await license_post(
                _license_post_url(license_input),
                challenge,
                license_input.headers,
            )
        except Exception as exc:
            raise ProofError(
                f"License POST failed: {type(exc).__name__}"
            ) from exc

        if not response_bytes:
            raise ProofError("License server returned an empty response.")

        # -- step 3: parse keys -----------------------------------------------
        try:
            raw_keys = session.parse_license_response(response_bytes)
        except Exception as exc:
            raise ProofError(
                f"Failed to parse license response: {type(exc).__name__}"
            ) from exc

        if not raw_keys:
            raise ProofError("License response contained no content keys.")

        keys = [ContentKey(kid=kid, key=key) for kid, key in raw_keys]
    finally:
        if session is not None:
            close = getattr(session, "close", None)
            if callable(close):
                close()

    # -- step 4: decrypt ------------------------------------------------------
    decrypted_paths: list[Path] = []
    validation_failed = False
    try:
        for index, encrypted in enumerate(encrypted_paths):
            part_output = output_path
            if len(encrypted_paths) > 1:
                part_output = output_path.with_name(
                    f".{output_path.stem}.decrypted-{index}{encrypted.suffix}"
                )
            decrypted_paths.append(
                await run_mp4decrypt(
                    input_path=encrypted,
                    output_path=part_output,
                    keys=keys,
                    overwrite=overwrite,
                )
            )

        required_streams: set[str] = set()
        if validate_output:
            try:
                for decrypted in decrypted_paths:
                    required_streams.update(await probe_stream_types(decrypted))
                if not required_streams:
                    raise DecryptError(
                        "ffprobe found no media streams in decrypted artifacts."
                    )
            except DecryptError:
                validation_failed = True
                raise

        if len(decrypted_paths) > 1:
            result_path = await run_ffmpeg_mux(
                decrypted_paths,
                output_path,
                overwrite=overwrite,
            )
        else:
            result_path = decrypted_paths[0]

        if validate_output:
            try:
                await validate_mp4_streams(result_path, required_streams)
            except DecryptError:
                validation_failed = True
                raise
    except DecryptError as exc:
        raise ProofError(f"Decryption failed: {exc}") from exc
    finally:
        if len(decrypted_paths) > 1:
            for decrypted in decrypted_paths:
                try:
                    decrypted.unlink()
                except OSError:
                    pass
        if validation_failed:
            _cleanup_decryption_outputs(output_path, decrypted_paths)

    return ProofResult(output_path=result_path, keys=keys)


def _license_post_url(license_input: WidevineLicenseInput) -> str:
    """Return the runtime URL used for license POSTs."""
    if license_input.license_url.rstrip("/") == UDEMY_WIDEVINE_PROXY_URL:
        return build_udemy_widevine_proxy_url(license_input)
    return license_input.license_url
