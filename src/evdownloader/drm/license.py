"""Widevine license input normalization.

Provides a safe normalization layer that merges provider-discovered DRM metadata
with CLI overrides, validates required fields, and exposes a redacted summary
that never leaks secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..models import DrmInfo

# Udemy's Widevine proxy endpoint (used when no override is provided).
UDEMY_WIDEVINE_PROXY_URL = "https://www.udemy.com/media-license-server/validate-auth-token"
_WIDEVINE_DRM_TYPE = "widevine"

_SENSITIVE_HEADER_PARTS = ("authorization", "cookie", "token", "secret", "key")
_SENSITIVE_QUERY_KEYS = frozenset({"auth_token", "token", "access_token", "key", "secret"})


def _is_sensitive_header(name: str) -> bool:
    """Return True if a header name likely carries secret material."""
    normalized = name.lower()
    return any(part in normalized for part in _SENSITIVE_HEADER_PARTS)


def _redact_url(url: str) -> str:
    """Redact known secret-bearing query parameters from a URL."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [
        (key, "***" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class LicenseInputError(ValueError):
    """Raised when Widevine license inputs are invalid or incomplete."""


@dataclass(frozen=True, slots=True)
class WidevineLicenseInput:
    """Normalized set of inputs needed to request a Widevine license."""

    license_url: str
    pssh: str
    key_id: str | None = None
    token: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    scheme: str = "widevine"

    # -- safe representation -------------------------------------------------

    def token_present(self) -> bool:
        """Return True if a token was provided."""
        return self.token is not None and self.token != ""

    def redacted_headers(self) -> dict[str, str]:
        """Return a copy of headers with sensitive values replaced by ``***``."""
        out: dict[str, str] = {}
        for k, v in self.headers.items():
            if _is_sensitive_header(k):
                out[k] = "***"
            else:
                out[k] = v
        return out

    def safe_summary(self) -> str:
        """Human-readable summary safe for logs (no secrets)."""
        hdrs = self.redacted_headers()
        hdr_part = ", ".join(f"{k}={v}" for k, v in sorted(hdrs.items())) if hdrs else "none"
        return (
            f"WidevineLicenseInput("
            f"scheme={self.scheme}, "
            f"license_url={_redact_url(self.license_url)}, "
            f"pssh=<len:{len(self.pssh)}>, "
            f"key_id={self.key_id or 'none'}, "
            f"token_present={self.token_present()}, "
            f"headers={{{hdr_part}}})"
        )


def normalize_widevine_license_input(
    drm: DrmInfo,
    *,
    default_license_url: str | None = None,
    override_license_url: str | None = None,
    override_token: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> WidevineLicenseInput:
    """Merge provider DRM info with CLI overrides into a validated input.

    Precedence (highest wins):
    - ``license_url``: *override_license_url* > ``drm.license_url`` > *default_license_url*
    - ``token``: *override_token* > ``drm.token``
    - ``headers``: ``drm.headers`` merged with *extra_headers* (extra wins on collision)

    Raises:
        LicenseInputError: if scheme is not widevine, pssh is missing, or
            license_url cannot be resolved.
    """
    if drm.scheme != "widevine":
        raise LicenseInputError(
            f"Expected scheme 'widevine', got '{drm.scheme}'. "
            "Only Widevine is supported at this layer."
        )

    pssh = drm.pssh
    if not pssh:
        raise LicenseInputError(
            "PSSH is required for Widevine license acquisition but was not "
            "provided by the DRM detection layer."
        )

    license_url = override_license_url or drm.license_url or default_license_url
    if not license_url:
        raise LicenseInputError(
            "No license URL available. Provide one via --drm-license-server, "
            "the manifest, or the provider default."
        )

    token = override_token if override_token else drm.token

    # Merge headers: provider first, extras on top (explicit overrides win).
    headers = dict(drm.headers)
    if extra_headers:
        headers.update(extra_headers)

    return WidevineLicenseInput(
        license_url=license_url,
        pssh=pssh,
        key_id=drm.key_id,
        token=token or None,
        headers=headers,
        scheme=drm.scheme,
    )


def build_udemy_widevine_proxy_url(license_input: WidevineLicenseInput) -> str:
    """Build Udemy's runtime Widevine proxy URL with ``auth_token``.

    The returned URL contains the token and must never be logged directly. Use
    ``safe_summary()`` or ``_redact_url()`` for human-readable output.
    """
    if license_input.scheme != "widevine":
        raise LicenseInputError(
            f"Expected scheme 'widevine', got '{license_input.scheme}'. "
            "Udemy proxy URL construction only supports Widevine."
        )
    if not license_input.token:
        raise LicenseInputError(
            "Udemy Widevine proxy requires a media license token. "
            "Resolve asset.media_license_token or pass --drm-token."
        )

    parts = urlsplit(license_input.license_url)
    existing = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"drm_type", "auth_token"}
    ]
    query = urlencode(
        [*existing, ("drm_type", _WIDEVINE_DRM_TYPE), ("auth_token", license_input.token)]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


# ---------------------------------------------------------------------------
# License POST boundary
# ---------------------------------------------------------------------------


class LicensePostError(Exception):
    """Raised when the license POST fails (network or non-2xx response)."""


async def post_license_challenge(
    url: str,
    challenge: bytes,
    headers: dict[str, str],
) -> bytes:
    """POST a raw Widevine license challenge and return the response bytes.

    Sends ``challenge`` as ``application/octet-stream`` (default) unless the
    caller already set ``Content-Type`` in *headers*.

    Raises:
        LicensePostError: on network error or non-2xx response.  The error
            message never includes the challenge body, token, or response.
    """
    import rnet

    from ..config import RNET_IMPERSONATE

    send_headers = dict(headers)
    if "Content-Type" not in send_headers and "content-type" not in send_headers:
        send_headers["Content-Type"] = "application/octet-stream"

    try:
        client = rnet.Client(impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None))
        resp = await client.post(url, body=challenge, headers=send_headers)
    except Exception as exc:
        raise LicensePostError(
            f"License POST network error: {type(exc).__name__}"
        ) from exc

    status = resp.status_code
    if not status.is_success():
        raise LicensePostError(
            f"License server returned HTTP {status.as_int()}"
        )

    return await resp.bytes()
