"""Tests for DRM detection and manifest parsing."""

from __future__ import annotations

from evdownloader.drm.detector import detect_drm
from evdownloader.drm.hls import parse_hls
from evdownloader.drm.mpd import parse_mpd

# ============================================================================
# MPD Tests
# ============================================================================

class TestMPDParsing:
    """Tests for DASH MPD manifest parsing."""

    def test_udemy_like_mpd_widevine_and_playready(self) -> None:
        """Udemy-like MPD with Widevine + PlayReady + default_KID returns
        both systems with PSSH and key_id."""
        mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     xmlns:cenc="urn:mpeg:cenc:2011"
     xmlns:mspr="urn:microsoft:playready"
     profiles="urn:mpeg:dash:profile:isoff-live:2011">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed">
        <cenc:pssh>AAAAMXBzc2gAAAAA7e+LqXnLsTaN+qMH2C6VnwAAAA...</cenc:pssh>
      </ContentProtection>
      <ContentProtection schemeIdUri="urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95">
        <cenc:pssh>AAAAMXBzc2gAAAAA7e+LqXnLsTaN+qMH2C6VnwAAAA...</cenc:pssh>
      </ContentProtection>
      <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"
                         cenc:default_KID="11223344-5566-7788-99aa-bbccddeeff00">
        <cenc:pssh>AAAAMXBzc2gAAAAA7e+LqXnLsTaN+qMH2C6VnwAAAA...</cenc:pssh>
      </ContentProtection>
    </AdaptationSet>
  </Period>
</MPD>"""
        results = parse_mpd(mpd)

        # Should find Widevine, PlayReady, and CENC
        schemes = [r.scheme for r in results]
        assert "widevine" in schemes
        assert "playready" in schemes
        assert "cenc" in schemes

        # Verify Widevine details
        widevine = next(r for r in results if r.scheme == "widevine")
        assert widevine.pssh is not None
        assert widevine.pssh.startswith("AAAA")
        assert widevine.key_id is not None
        assert widevine.key_id == "11223344-5566-7788-99aa-bbccddeeff00"

        # Verify PlayReady details
        playready = next(r for r in results if r.scheme == "playready")
        assert playready.pssh is not None

    def test_clear_mpd_returns_no_drm(self) -> None:
        """MPD without ContentProtection elements returns empty list."""
        mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     profiles="urn:mpeg:dash:profile:isoff-live:2011">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="1" bandwidth="1000000"/>
    </AdaptationSet>
  </Period>
</MPD>"""
        results = parse_mpd(mpd)
        assert results == []

    def test_invalid_xml_returns_empty(self) -> None:
        """Malformed XML returns empty list without raising."""
        results = parse_mpd("not xml at all <><><>")
        assert results == []

    def test_playready_la_url_extraction(self) -> None:
        """PlayReady LA_URL is extracted from mspr:pro when present."""
        mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     xmlns:cenc="urn:mpeg:cenc:2011"
     xmlns:mspr="urn:microsoft:playready">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <ContentProtection schemeIdUri="urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95">
        <mspr:pro>
          <LA_URL>https://license.example.com/playready</LA_URL>
        </mspr:pro>
      </ContentProtection>
    </AdaptationSet>
  </Period>
</MPD>"""
        results = parse_mpd(mpd)
        playready = next(r for r in results if r.scheme == "playready")
        assert playready.license_url == "https://license.example.com/playready"

    def test_widevine_pssh_extraction(self) -> None:
        """Widevine PSSH is correctly extracted."""
        pssh_data = "AAAAMXBzc2gAAAAA7e+LqXnLsTaN+qMH2C6VnwAAAA=="
        mpd = f"""<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     xmlns:cenc="urn:mpeg:cenc:2011">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed">
        <cenc:pssh>{pssh_data}</cenc:pssh>
      </ContentProtection>
    </AdaptationSet>
  </Period>
</MPD>"""
        results = parse_mpd(mpd)
        widevine = next(r for r in results if r.scheme == "widevine")
        assert widevine.pssh == pssh_data

    def test_udemy_style_cenc_default_kid_propagates_to_widevine(self) -> None:
        """Udemy puts default_KID on the generic CENC marker, not Widevine."""
        mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     xmlns:cenc="urn:mpeg:cenc:2011">
  <Period><AdaptationSet mimeType="video/mp4">
    <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"
                       cenc:default_KID="fbf0dce4-2f8b-48b2-9229-1629595c0170"/>
    <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed">
      <cenc:pssh>AAAAV3Bzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAADc=</cenc:pssh>
    </ContentProtection>
  </AdaptationSet></Period>
</MPD>"""
        results = parse_mpd(mpd)
        widevine = next(r for r in results if r.scheme == "widevine")
        cenc = next(r for r in results if r.scheme == "cenc")
        assert widevine.key_id == "fbf0dce4-2f8b-48b2-9229-1629595c0170"
        assert cenc.key_id == "fbf0dce4-2f8b-48b2-9229-1629595c0170"


# ============================================================================
# HLS Tests
# ============================================================================

class TestHLSParsing:
    """Tests for HLS m3u8 manifest parsing."""

    def test_aes128_detection(self) -> None:
        """METHOD=AES-128 is classified as aes-128, not DRM circumvention."""
        m3u8 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key.bin"
#EXTINF:10.0,
segment1.ts
#EXTINF:10.0,
segment2.ts
"""
        results = parse_hls(m3u8)
        assert len(results) == 1
        assert results[0].scheme == "aes-128"
        assert results[0].license_url == "https://example.com/key.bin"

    def test_fairplay_detection(self) -> None:
        """KEYFORMAT=com.apple.streamingkeydelivery is classified as fairplay."""
        m3u8 = """#EXTM3U
#EXT-X-VERSION:5
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key-id",KEYFORMAT="com.apple.streamingkeydelivery"
#EXTINF:10.0,
segment1.ts
"""
        results = parse_hls(m3u8)
        assert len(results) == 1
        assert results[0].scheme == "fairplay"
        assert results[0].license_url == "skd://key-id"

    def test_hls_key_attributes_can_appear_in_any_order(self) -> None:
        """HLS EXT-X-KEY attribute order is not guaranteed."""
        m3u8 = """#EXTM3U
#EXT-X-KEY:URI="skd://key-id",KEYFORMAT="com.apple.streamingkeydelivery",METHOD=SAMPLE-AES
#EXTINF:10.0,
segment1.ts
"""
        results = parse_hls(m3u8)
        assert len(results) == 1
        assert results[0].scheme == "fairplay"
        assert results[0].license_url == "skd://key-id"

    def test_unknown_keyformat_preserved(self) -> None:
        """Unknown KEYFORMAT is preserved as a custom scheme label."""
        m3u8 = """#EXTM3U
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="https://example.com/key",KEYFORMAT="com.example.custom"
#EXTINF:10.0,
segment1.ts
"""
        results = parse_hls(m3u8)
        assert len(results) == 1
        assert results[0].scheme == "com.example.custom"

    def test_none_method_skipped(self) -> None:
        """METHOD=NONE entries are skipped."""
        m3u8 = """#EXTM3U
#EXT-X-KEY:METHOD=NONE
#EXTINF:10.0,
segment1.ts
"""
        results = parse_hls(m3u8)
        assert results == []

    def test_multiple_keys_deduplicated(self) -> None:
        """Multiple EXT-X-KEY with same method are deduplicated."""
        m3u8 = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key1.bin"
#EXTINF:10.0,
segment1.ts
#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key2.bin"
#EXTINF:10.0,
segment2.ts
"""
        results = parse_hls(m3u8)
        assert len(results) == 1
        assert results[0].scheme == "aes-128"

    def test_sample_aes_without_keyformat(self) -> None:
        """SAMPLE-AES without FairPlay keyformat is classified as sample-aes."""
        m3u8 = """#EXTM3U
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="https://example.com/key"
#EXTINF:10.0,
segment1.ts
"""
        results = parse_hls(m3u8)
        assert len(results) == 1
        assert results[0].scheme == "sample-aes"


# ============================================================================
# Detector Tests
# ============================================================================

class TestDetector:
    """Tests for the top-level detect_drm function."""

    def test_auto_detect_mpd_by_extension(self) -> None:
        """MPD type is inferred from URL extension."""
        mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     xmlns:cenc="urn:mpeg:cenc:2011">
  <Period><AdaptationSet mimeType="video/mp4">
    <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed">
      <cenc:pssh>AAAAXA==</cenc:pssh>
    </ContentProtection>
  </AdaptationSet></Period>
</MPD>"""
        result = detect_drm(mpd, url="https://example.com/video.mpd?token=abc")
        assert result.manifest_type == "mpd"
        assert len(result.systems) == 1
        assert result.systems[0].scheme == "widevine"

    def test_auto_detect_hls_by_extension(self) -> None:
        """HLS type is inferred from URL extension."""
        m3u8 = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key"
#EXTINF:10.0,
segment.ts
"""
        result = detect_drm(m3u8, url="https://example.com/playlist.m3u8")
        assert result.manifest_type == "m3u8"
        assert len(result.systems) == 1
        assert result.systems[0].scheme == "aes-128"

    def test_auto_detect_by_content_type(self) -> None:
        """Content-Type header is used for type inference."""
        mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period><AdaptationSet mimeType="video/mp4">
    <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
  </AdaptationSet></Period>
</MPD>"""
        result = detect_drm(mpd, content_type="application/dash+xml")
        assert result.manifest_type == "mpd"

    def test_unknown_manifest_returns_empty(self) -> None:
        """Unrecognizable content returns empty result."""
        result = detect_drm("random text", url="https://example.com/file")
        assert result.manifest_type == "unknown"
        assert result.systems == []
