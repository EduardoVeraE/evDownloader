"""Filesystem publication primitives; no provider, network, or result policy."""

import contextlib
import os
import tempfile
from pathlib import Path

import aiofiles


def artifact_path(base: Path, suffix: str) -> Path:
    """Append a suffix without replacing dots in a lesson's title."""
    return base.parent / f"{base.name}{suffix}"


def publish_atomic_text(path: Path, content: str) -> None:
    """Publish a complete UTF-8 sidecar, cleaning staging on any failure."""
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            with contextlib.suppress(OSError):
                Path(temporary_name).unlink()


async def write_text(path: Path, content: str) -> None:
    """Preserve the existing direct-write behavior for optional extras."""
    async with aiofiles.open(path, "w", encoding="utf-8") as output:
        await output.write(content)
