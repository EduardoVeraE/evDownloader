"""Canonical URL authority parsing and provider trust policies."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_CREDENTIAL_HEADER_PARTS = ("authorization", "cookie", "token", "secret", "key")


@dataclass(frozen=True, slots=True)
class URLAuthority:
    """Normalized authority components relevant to credential decisions."""

    scheme: str
    hostname: str
    port: int
    explicit_port: bool


def normalize_hostname(hostname: str) -> str | None:
    """Return a validated ASCII/IDNA hostname, or ``None`` when malformed."""
    if not isinstance(hostname, str) or not hostname or hostname.endswith(".."):
        return None
    hostname = hostname.removesuffix(".")
    if not hostname or any(char.isspace() or char == "\\" for char in hostname):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            normalized = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
        labels = normalized.split(".")
        if len(normalized) > 253 or not all(
            label
            and len(label) <= 63
            and label[0] != "-"
            and label[-1] != "-"
            and all(char.isascii() and (char.isalnum() or char == "-") for char in label)
            for label in labels
        ):
            return None
        return normalized
    return address.compressed.lower()


def parse_url_authority(url: str) -> URLAuthority | None:
    """Parse a safe HTTPS URL without accepting ambiguous authority syntax."""
    if (
        not isinstance(url, str)
        or not url
        or any(
            char.isspace() or char == "\\" or ord(char) < 0x20 or ord(char) == 0x7F for char in url
        )
    ):
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parsed_port = parsed.port
    except TypeError, ValueError, UnicodeError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or host is None
        or host.endswith(".")
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or "%" in parsed.netloc
    ):
        return None
    normalized_host = normalize_hostname(host)
    if normalized_host is None or parsed_port == 0:
        return None
    if parsed.netloc.startswith("["):
        port_suffix = parsed.netloc.partition("]")[2]
        if port_suffix == ":":
            return None
        explicit_port = port_suffix.startswith(":")
    else:
        if parsed.netloc.endswith(":"):
            return None
        explicit_port = ":" in parsed.netloc
    return URLAuthority(
        scheme="https",
        hostname=normalized_host,
        port=parsed_port if parsed_port is not None else 443,
        explicit_port=explicit_port,
    )


def hostname_matches(hostname: str, suffixes: tuple[str, ...]) -> bool:
    """Match exact hosts or DNS-label-boundary suffixes after normalization."""
    normalized = normalize_hostname(hostname)
    if normalized is None:
        return False
    try:
        normalized_ip = ipaddress.ip_address(normalized)
    except ValueError:
        normalized_ip = None
    for suffix in suffixes:
        trusted = normalize_hostname(suffix)
        if trusted is None:
            continue
        if normalized == trusted:
            return True
        try:
            trusted_ip = ipaddress.ip_address(trusted)
        except ValueError:
            trusted_ip = None
        if normalized_ip is None and trusted_ip is None and normalized.endswith(f".{trusted}"):
            return True
    return False


@dataclass(frozen=True, slots=True)
class URLPolicy:
    """Allowed HTTPS authorities for one provider-facing trust boundary."""

    label: str
    host_suffixes: tuple[str, ...]
    ports: frozenset[int] = field(default_factory=lambda: frozenset({443}))

    def allows_authority(self, authority: URLAuthority | None) -> bool:
        return bool(
            authority is not None
            and authority.port in self.ports
            and hostname_matches(authority.hostname, self.host_suffixes)
        )

    def allows(self, url: str) -> bool:
        return self.allows_authority(parse_url_authority(url))

    def allows_hostname(self, hostname: str) -> bool:
        return hostname_matches(hostname.lstrip("."), self.host_suffixes)


def safe_url_label(url: str) -> str:
    """Return a non-identifying label suitable for errors and diagnostics."""
    authority = parse_url_authority(url)
    if authority is None:
        return "invalid"
    for label, suffixes in (
        ("udemy", ("udemy.com",)),
        ("platzi", ("platzi.com",)),
        ("codigofacilito", ("codigofacilito.com",)),
    ):
        if URLPolicy(label, suffixes).allows_authority(authority):
            return label
    return "external"


def is_credential_header(name: str) -> bool:
    """Identify headers whose values must remain authority-scoped."""
    normalized = name.lower()
    return any(part in normalized for part in _CREDENTIAL_HEADER_PARTS)
