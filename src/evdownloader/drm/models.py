"""DRM detection result model."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import DrmInfo


class DrmDetectionResult(BaseModel):
    """Result of DRM detection from a manifest.

    Wraps the list of DRM systems found along with optional context
    about the detection source.
    """

    systems: list[DrmInfo] = Field(default_factory=list)
    manifest_type: str = ""  # "mpd" or "m3u8"
    raw_pssh: list[str] = Field(default_factory=list)
