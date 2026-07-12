"""Widevine CDM boundary — optional pywidevine integration.

This module wraps pywidevine behind a defensive boundary.  pywidevine is an
*optional* dependency: if it is not installed, every public function raises
:class:`CdmUnavailableError` with an actionable message.  No secrets are
logged or stored; the ``.wvd`` device file is loaded only on demand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class CdmUnavailableError(Exception):
    """Raised when pywidevine is not installed or a device file is invalid."""


@dataclass
class WidevineCdmSession:
    """Thin wrapper around a pywidevine CDM session.

    The ``device_path`` is stored but never loaded until :meth:`open` is
    called.  All cryptographic work is delegated to pywidevine.
    """

    device_path: Path
    _state: tuple[Any, Any] | None = field(default=None, repr=False, compare=False)

    # -- public API -----------------------------------------------------------

    def open(self) -> WidevineCdmSession:
        """Load the device file and return ``self`` for chaining.

        Raises:
            CdmUnavailableError: if pywidevine is missing or the device is
                invalid.
        """
        self._state = self._load_device()
        return self

    def close(self) -> None:
        """Close the underlying pywidevine session, if opened."""
        if self._state is None:
            return
        cdm_obj, session_id = self._state
        close = getattr(cdm_obj, "close", None)
        if callable(close):
            close(session_id)
        self._state = None

    def generate_challenge(self, pssh: str) -> bytes:
        """Generate a license challenge from a PSSH string.

        Args:
            pssh: Base64-encoded PSSH box.

        Returns:
            Raw challenge bytes suitable for a Widevine license POST.
        """
        if self._state is None:
            raise CdmUnavailableError("Session is not open — call open() first.")
        cdm_obj, session_id = self._state
        _, _, PSSH = _load_pywidevine()
        try:
            pssh_obj = PSSH(pssh)
            return cdm_obj.get_license_challenge(session_id, pssh_obj)
        except Exception as exc:
            raise CdmUnavailableError("Failed to generate Widevine challenge.") from exc

    def parse_license_response(self, license_bytes: bytes) -> list[tuple[str, str]]:
        """Parse a license response and return ``(kid_hex, key_hex)`` pairs.

        Args:
            license_bytes: Raw bytes of the license server response.

        Returns:
            List of (kid_hex, key_hex) tuples with lower-case hex strings.
        """
        if self._state is None:
            raise CdmUnavailableError("Session is not open — call open() first.")
        cdm_obj, session_id = self._state
        keys: list[tuple[str, str]] = []
        try:
            cdm_obj.parse_license(session_id, license_bytes)
            bound_keys = cdm_obj.get_keys(session_id)
        except Exception as exc:
            raise CdmUnavailableError("Failed to parse Widevine license response.") from exc
        for bound in bound_keys:
            if getattr(bound, "type", None) not in (None, "CONTENT"):
                continue
            kid_hex = bound.kid.hex()
            key_hex = bound.key.hex()
            keys.append((kid_hex, key_hex))
        return keys

    # -- internals ------------------------------------------------------------

    def _load_device(self) -> tuple[Any, Any]:
        """Import pywidevine and load the device file."""
        if not self.device_path.is_file():
            raise CdmUnavailableError(
                f"Device file not found: {self.device_path}.  "
                "Provide a valid .wvd device file."
            )
        Cdm, Device, _ = _load_pywidevine()
        try:
            device = Device.load(self.device_path)
        except Exception as exc:
            raise CdmUnavailableError(
                f"Failed to load device file: {self.device_path}.  "
                "Ensure it is a valid .wvd file."
            ) from exc

        try:
            cdm = Cdm.from_device(device)
            session_id = cdm.open()
        except Exception as exc:
            raise CdmUnavailableError("Failed to initialise CDM from device.") from exc

        return (cdm, session_id)


def _load_pywidevine() -> tuple[type[Any], type[Any], type[Any]]:
    """Load pywidevine classes without making pywidevine a hard dependency."""
    try:
        from pywidevine.cdm import Cdm  # type: ignore[import-not-found]
        from pywidevine.device import Device  # type: ignore[import-not-found]
        from pywidevine.pssh import PSSH  # type: ignore[import-not-found]
    except ImportError:
        raise CdmUnavailableError(
            "pywidevine is not installed. Install the optional DRM extras with: "
            "pip install evDownloader[drm]"
        ) from None
    return Cdm, Device, PSSH
