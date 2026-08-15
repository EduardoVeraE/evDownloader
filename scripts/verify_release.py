#!/usr/bin/env python3
"""Validate release source state, tags, and Python distributions."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import re
import subprocess
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TAG_PATTERN = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


ValidationError = ValueError


def _project() -> tuple[str, str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    name = data["project"]["name"]
    version = data["project"]["version"]
    if not isinstance(name, str) or not isinstance(version, str):
        raise ValidationError("project.name and project.version must be strings")
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    return normalized, version


def verify_tag(tag: str) -> None:
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ValidationError(f"release tag must have the exact form vX.Y.Z: {tag!r}")
    _, version = _project()
    if tag[1:] != version:
        raise ValidationError(f"tag {tag!r} does not match project.version {version!r}")


def verify_source() -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValidationError(result.stderr.strip() or "git status failed")
    if result.stdout:
        raise ValidationError(f"release source tree is dirty:\n{result.stdout.rstrip()}")


def _validate_archive_names(names: list[str], archive: Path) -> None:
    if len(names) != len(set(names)):
        raise ValidationError(f"{archive.name} contains duplicate paths")
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValidationError(f"{archive.name} contains unsafe path {name!r}")
        if any(part in {".env", ".git", "__pycache__"} for part in path.parts):
            raise ValidationError(f"{archive.name} contains forbidden path {name!r}")
        if path.suffix in {".pyc", ".pyo"}:
            raise ValidationError(f"{archive.name} contains bytecode {name!r}")


def _verify_metadata(payload: bytes, expected_name: str, version: str, source: str) -> None:
    metadata = BytesParser(policy=default).parsebytes(payload)
    actual_name = re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower()
    if actual_name != expected_name:
        raise ValidationError(f"{source} has unexpected Name metadata {actual_name!r}")
    if metadata.get("Version") != version:
        raise ValidationError(f"{source} has unexpected Version metadata")
    if metadata.get("Requires-Python") != ">=3.14":
        raise ValidationError(f"{source} does not preserve Python >=3.14 support")


def _source_files(package: str) -> dict[str, bytes]:
    source_root = ROOT / "src" / package
    files: dict[str, bytes] = {}
    for path in source_root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        relative = path.relative_to(source_root).as_posix()
        files[f"{package}/{relative}"] = path.read_bytes()
    return files


def _verify_record(files: dict[str, bytes], record_path: str, archive: Path) -> None:
    rows = list(csv.reader(io.StringIO(files[record_path].decode("utf-8"))))
    if len(rows) != len(files) or len({row[0] for row in rows}) != len(rows):
        raise ValidationError(f"{archive.name} RECORD paths are incomplete or duplicated")
    records = {row[0]: row[1:] for row in rows}
    if set(records) != set(files):
        raise ValidationError(f"{archive.name} RECORD does not match wheel contents")
    for name, payload in files.items():
        digest, size = records[name]
        if name == record_path:
            if digest or size:
                raise ValidationError(f"{archive.name} RECORD must not hash itself")
            continue
        encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        if digest != f"sha256={encoded}" or size != str(len(payload)):
            raise ValidationError(f"{archive.name} RECORD mismatch for {name}")


def _verify_wheel(path: Path, package: str, version: str) -> None:
    dist_info = f"{package}-{version}.dist-info"
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        _validate_archive_names([info.filename for info in infos], path)
        bad_file = archive.testzip()
        if bad_file is not None:
            raise ValidationError(f"{path.name} has a corrupt member: {bad_file}")
        if any(info.is_dir() for info in infos):
            raise ValidationError(f"{path.name} contains unexpected directory records")
        files = {info.filename: archive.read(info) for info in infos}

    source_files = _source_files(package)
    packaged_sources = {
        name: payload for name, payload in files.items() if name.startswith(f"{package}/")
    }
    if packaged_sources != source_files:
        raise ValidationError(f"{path.name} package files do not match src/{package}")

    expected_metadata = {
        f"{dist_info}/{name}"
        for name in ("METADATA", "WHEEL", "entry_points.txt", "licenses/LICENSE", "RECORD")
    }
    if set(files) != set(source_files) | expected_metadata:
        raise ValidationError(f"{path.name} contains unexpected or missing wheel metadata")
    _verify_metadata(files[f"{dist_info}/METADATA"], package, version, path.name)
    entry_points = files[f"{dist_info}/entry_points.txt"].decode("utf-8")
    for expected in ("evd = evdownloader.cli:app", "evdownloader = evdownloader.cli:app"):
        if expected not in entry_points:
            raise ValidationError(f"{path.name} is missing console script {expected!r}")
    _verify_record(files, f"{dist_info}/RECORD", path)


def _verify_sdist(path: Path, package: str, version: str) -> None:
    root = f"{package}-{version}"
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        _validate_archive_names([member.name for member in members], path)
        if any(not member.isfile() for member in members):
            raise ValidationError(f"{path.name} contains links or non-file members")
        files = {member.name: archive.extractfile(member).read() for member in members}  # type: ignore[union-attr]

    source_files = _source_files(package)
    expected = {f"{root}/src/{name}": payload for name, payload in source_files.items()}
    for name in (".gitignore", "LICENSE", "README.md", "pyproject.toml", "reqs.txt"):
        expected[f"{root}/{name}"] = (ROOT / name).read_bytes()
    expected_pkg_info = f"{root}/PKG-INFO"
    if set(files) != set(expected) | {expected_pkg_info}:
        raise ValidationError(f"{path.name} contains unexpected or missing sdist files")
    for name, payload in expected.items():
        if files[name] != payload:
            raise ValidationError(f"{path.name} does not match the source file for {name}")
    _verify_metadata(files[expected_pkg_info], package, version, path.name)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _distribution_files(directory: Path) -> tuple[Path, Path]:
    package, version = _project()
    wheel = directory / f"{package}-{version}-py3-none-any.whl"
    sdist = directory / f"{package}-{version}.tar.gz"
    actual = {path for path in directory.iterdir() if path.is_file()}
    if actual != {wheel, sdist}:
        raise ValidationError(f"{directory} must contain exactly {wheel.name} and {sdist.name}")
    return wheel, sdist


def verify_artifacts(directory: Path, compare: Path | None, sdist_wheel: Path | None) -> None:
    package, version = _project()
    wheel, sdist = _distribution_files(directory)
    _verify_wheel(wheel, package, version)
    _verify_sdist(sdist, package, version)

    if compare is not None:
        repeat_wheel, repeat_sdist = _distribution_files(compare)
        for first, second in ((wheel, repeat_wheel), (sdist, repeat_sdist)):
            if _sha256(first) != _sha256(second):
                raise ValidationError(f"repeated build differs for {first.name}")
    if sdist_wheel is not None:
        rebuilt = sdist_wheel / wheel.name
        actual = {path for path in sdist_wheel.iterdir() if path.is_file()}
        if actual != {rebuilt} or _sha256(wheel) != _sha256(rebuilt):
            raise ValidationError("wheel rebuilt from the sdist differs from the release wheel")

    print(f"{wheel.name} sha256:{_sha256(wheel)}")
    print(f"{sdist.name} sha256:{_sha256(sdist)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    tag_parser = subparsers.add_parser("tag")
    tag_parser.add_argument("tag")
    subparsers.add_parser("source")
    artifacts_parser = subparsers.add_parser("artifacts")
    artifacts_parser.add_argument("directory", type=Path)
    artifacts_parser.add_argument("--compare", type=Path)
    artifacts_parser.add_argument("--sdist-wheel", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "tag":
            verify_tag(args.tag)
            print(f"validated release tag {args.tag}")
        elif args.command == "source":
            verify_source()
            print("validated clean release source tree")
        else:
            verify_artifacts(args.directory, args.compare, args.sdist_wheel)
    except (KeyError, OSError, ValidationError, tarfile.TarError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"release validation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
