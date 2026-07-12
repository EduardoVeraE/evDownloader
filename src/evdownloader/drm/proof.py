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

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cdm import CdmUnavailableError, WidevineCdmSession
from .decrypt import ContentKey, DecryptError, run_mp4decrypt
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


# Type alias for the injectable license POST function.
LicensePostFn = Callable[
    [str, bytes, dict[str, str]],
    Coroutine[Any, Any, bytes],
]


async def prove_decrypt_path(
    *,
    license_input: WidevineLicenseInput,
    device_path: Path,
    encrypted_path: Path,
    output_path: Path,
    license_post: LicensePostFn,
    cdm_session_cls: type[WidevineCdmSession] = WidevineCdmSession,
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
    if not encrypted_path.is_file():
        raise ProofError(f"Encrypted file not found: {encrypted_path}")

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
    try:
        result_path = await run_mp4decrypt(
            input_path=encrypted_path,
            output_path=output_path,
            keys=keys,
        )
    except DecryptError as exc:
        raise ProofError(f"Decryption failed: {exc}") from exc

    return ProofResult(output_path=result_path, keys=keys)


def _license_post_url(license_input: WidevineLicenseInput) -> str:
    """Return the runtime URL used for license POSTs."""
    if license_input.license_url.rstrip("/") == UDEMY_WIDEVINE_PROXY_URL:
        return build_udemy_widevine_proxy_url(license_input)
    return license_input.license_url
