"""DRM detection, license-input, and decrypt proof APIs."""

from .cdm import CdmUnavailableError, WidevineCdmSession
from .decrypt import ContentKey, DecryptError, build_mp4decrypt_command, run_mp4decrypt
from .detector import detect_drm
from .license import (
    UDEMY_WIDEVINE_PROXY_URL,
    LicenseInputError,
    LicensePostError,
    WidevineLicenseInput,
    build_udemy_widevine_proxy_url,
    normalize_widevine_license_input,
    post_license_challenge,
)
from .models import DrmDetectionResult
from .proof import ProofError, ProofResult, prove_decrypt_path

__all__ = [
    "CdmUnavailableError",
    "ContentKey",
    "DecryptError",
    "DrmDetectionResult",
    "LicenseInputError",
    "LicensePostError",
    "ProofError",
    "ProofResult",
    "UDEMY_WIDEVINE_PROXY_URL",
    "WidevineCdmSession",
    "WidevineLicenseInput",
    "build_mp4decrypt_command",
    "build_udemy_widevine_proxy_url",
    "detect_drm",
    "normalize_widevine_license_input",
    "post_license_challenge",
    "prove_decrypt_path",
    "run_mp4decrypt",
]
