from __future__ import annotations

import io
import tarfile
import warnings
import zipfile
from pathlib import Path

import pytest

from scripts import verify_release


def test_wheel_rejects_duplicate_members_before_content_validation(tmp_path: Path) -> None:
    wheel = tmp_path / "duplicate.whl"
    with zipfile.ZipFile(wheel, "w") as archive, warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        archive.writestr("duplicate.txt", b"first")
        archive.writestr("duplicate.txt", b"second")

    with pytest.raises(ValueError, match="duplicate.whl contains duplicate paths"):
        verify_release._verify_wheel(wheel, "evdownloader", "0.1.3")


def test_sdist_rejects_duplicate_members_before_content_validation(tmp_path: Path) -> None:
    sdist = tmp_path / "duplicate.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for payload in (b"first", b"second"):
            member = tarfile.TarInfo("duplicate.txt")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(ValueError, match="duplicate.tar.gz contains duplicate paths"):
        verify_release._verify_sdist(sdist, "evdownloader", "0.1.3")
