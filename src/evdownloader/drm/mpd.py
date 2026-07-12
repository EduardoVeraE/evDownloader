"""DASH MPD manifest parser for DRM detection."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ..models import DrmInfo

# Well-known DRM UUIDs
_WIDEVINE_UUID = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
_PLAYREADY_UUID = "9a04f079-9840-4286-ab92-e65be0885f95"
_CENC_MPEG = "urn:mpeg:dash:mp4protection:2011"


def _local_name(name: str) -> str:
    """Return the XML local name for a possibly namespaced tag or attribute."""
    return name.rsplit("}", 1)[-1] if name.startswith("{") else name


def _get_attr_by_local_name(elem: ET.Element, local_name: str) -> str | None:
    """Read an attribute by local name, regardless of namespace prefix."""
    for key, value in elem.attrib.items():
        if _local_name(key) == local_name:
            return value.strip() if value else None
    return None


def _extract_la_url_from_pro(pro_elem: ET.Element) -> str | None:
    """Extract LA_URL from a mspr:pro element, if present.

    Returns the license URL only if it appears as a straightforward
    attribute or text content. Returns None on any parsing ambiguity.
    """
    # Check for LA_URL attribute
    la_url = pro_elem.get("LA_URL")
    if la_url:
        return la_url

    # Check for LA_URL as a direct child element (try multiple namespace variants)
    # LA_URL may inherit the default namespace or be in no namespace
    for tag in [
        "LA_URL",
        "{urn:mpeg:dash:schema:mpd:2011}LA_URL",
        "{urn:microsoft:playready}LA_URL",
    ]:
        la_elem = pro_elem.find(tag)
        if la_elem is not None and la_elem.text:
            return la_elem.text.strip()

    return None


def _find_content_protection(elem: ET.Element) -> list[ET.Element]:
    """Find all ContentProtection elements, handling both namespaced and non-namespaced."""
    results = []
    for child in elem.iter():
        if _local_name(child.tag) == "ContentProtection":
            results.append(child)
    return results


def _find_child(parent: ET.Element, local_name: str) -> ET.Element | None:
    """Find a child element by local name, handling namespaces."""
    for child in parent.iter():
        if _local_name(child.tag) == local_name:
            return child
    return None


def _default_key_id(adaptation_set: ET.Element) -> str | None:
    """Extract the common CENC default_KID for an adaptation set, if present."""
    for cp in _find_content_protection(adaptation_set):
        scheme = cp.get("schemeIdUri", "")
        if _CENC_MPEG.lower() in scheme.lower():
            kid = _get_attr_by_local_name(cp, "default_KID")
            if kid:
                return kid
    for cp in _find_content_protection(adaptation_set):
        kid = _get_attr_by_local_name(cp, "default_KID")
        if kid:
            return kid
    return None


def _detect_widevine(adaptation_set: ET.Element, fallback_key_id: str | None) -> list[DrmInfo]:
    """Detect Widevine DRM from ContentProtection elements."""
    results: list[DrmInfo] = []

    for cp in _find_content_protection(adaptation_set):
        scheme = cp.get("schemeIdUri", "")
        if _WIDEVINE_UUID.lower() in scheme.lower():
            pssh_elem = _find_child(cp, "pssh")
            pssh = pssh_elem.text.strip() if pssh_elem is not None and pssh_elem.text else None

            kid = _get_attr_by_local_name(cp, "default_KID") or fallback_key_id

            results.append(
                DrmInfo(
                    scheme="widevine",
                    pssh=pssh,
                    key_id=kid,
                )
            )

    return results


def _detect_playready(adaptation_set: ET.Element) -> list[DrmInfo]:
    """Detect PlayReady DRM from ContentProtection elements."""
    results: list[DrmInfo] = []

    for cp in _find_content_protection(adaptation_set):
        scheme = cp.get("schemeIdUri", "")
        if _PLAYREADY_UUID.lower() in scheme.lower():
            pssh_elem = _find_child(cp, "pssh")
            pssh = pssh_elem.text.strip() if pssh_elem is not None and pssh_elem.text else None

            # Try to extract LA_URL from mspr:pro
            license_url = None
            for pro in cp.iter("{urn:microsoft:playready}pro"):
                license_url = _extract_la_url_from_pro(pro)
                if license_url:
                    break
            # Also try non-namespaced pro
            if not license_url:
                for pro in cp.iter("pro"):
                    license_url = _extract_la_url_from_pro(pro)
                    if license_url:
                        break

            results.append(
                DrmInfo(
                    scheme="playready",
                    license_url=license_url,
                    pssh=pssh,
                )
            )

    return results


def _detect_cenc(adaptation_set: ET.Element) -> list[DrmInfo]:
    """Detect generic CENC MPEG protection (not a DRM system itself)."""
    results: list[DrmInfo] = []

    for cp in _find_content_protection(adaptation_set):
        scheme = cp.get("schemeIdUri", "")
        if _CENC_MPEG.lower() in scheme.lower():
            pssh_elem = _find_child(cp, "pssh")
            pssh = pssh_elem.text.strip() if pssh_elem is not None and pssh_elem.text else None

            kid = _get_attr_by_local_name(cp, "default_KID")

            results.append(
                DrmInfo(
                    scheme="cenc",
                    pssh=pssh,
                    key_id=kid,
                )
            )

    return results


def _is_adaptation_set(elem: ET.Element) -> bool:
    """Check if an element is an AdaptationSet."""
    return _local_name(elem.tag) == "AdaptationSet"


def parse_mpd(manifest_text: str) -> list[DrmInfo]:
    """Parse a DASH MPD manifest and extract DRM information.

    Scans all ContentProtection elements across all adaptation sets
    and returns a list of DrmInfo entries for each DRM system found.

    Args:
        manifest_text: The raw MPD XML content.

    Returns:
        List of DrmInfo objects, one per DRM system detected.
    """
    try:
        root = ET.fromstring(manifest_text)
    except ET.ParseError:
        return []

    # Deduplicate results by scheme
    seen_schemes: set[str] = set()
    results: list[DrmInfo] = []

    # Scan all adaptation sets for ContentProtection
    for elem in root.iter():
        if _is_adaptation_set(elem):
            fallback_key_id = _default_key_id(elem)

            # Widevine
            for info in _detect_widevine(elem, fallback_key_id):
                if info.scheme not in seen_schemes:
                    seen_schemes.add(info.scheme)
                    results.append(info)

            # PlayReady
            for info in _detect_playready(elem):
                if info.scheme not in seen_schemes:
                    seen_schemes.add(info.scheme)
                    results.append(info)

            # CENC (generic marker)
            for info in _detect_cenc(elem):
                if "cenc" not in seen_schemes:
                    seen_schemes.add("cenc")
                    results.append(info)

    return results
