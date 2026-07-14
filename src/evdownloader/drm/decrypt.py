"""Decryption boundary — ContentKey, mp4decrypt command builder and runner.

This module is the only place that touches ``mp4decrypt``.  It never logs or
exposes key material in exceptions; the command is redacted in error messages.
Partial output files are written to a temporary path and renamed only on
success so that a failed run never leaves a corrupt final artifact.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class DecryptError(Exception):
    """Raised when decryption fails for any reason."""


@dataclass(frozen=True, slots=True)
class ContentKey:
    """A single content key identified by its Key ID (KID).

    Both fields are hex strings (lower-case, no prefix).
    """

    kid: str
    key: str

    def __post_init__(self) -> None:
        """Validate hex format."""
        if len(self.kid) != 32 or not all(c in "0123456789abcdef" for c in self.kid):
            raise ValueError("KID must be a 16-byte hex string, got: <redacted>")
        if len(self.key) != 32 or not all(c in "0123456789abcdef" for c in self.key):
            raise ValueError("KEY must be a 16-byte hex string, got: <redacted>")


# ---------------------------------------------------------------------------
# mp4decrypt command builder
# ---------------------------------------------------------------------------


def build_mp4decrypt_command(
    input_path: str | Path,
    output_path: str | Path,
    keys: list[ContentKey],
    *,
    overwrite: bool = False,
) -> list[str]:
    """Build the ``mp4decrypt`` argument list.

    Args:
        input_path: Path to the encrypted ``.mp4`` file.
        output_path: Desired path for the decrypted output.
        keys: At least one :class:`ContentKey`.
        overwrite: Retained for compatibility; output replacement is handled
            by the Python runner before atomic promotion.

    Returns:
        Argument list suitable for :func:`asyncio.create_subprocess_exec`.

    Raises:
        DecryptError: if *keys* is empty.
    """
    if not keys:
        raise DecryptError("At least one content key is required for decryption.")

    cmd: list[str] = ["mp4decrypt"]
    for ck in keys:
        cmd.extend(["--key", f"{ck.kid}:{ck.key}"])
    cmd.append(str(input_path))
    cmd.append(str(output_path))
    return cmd


def _redact_cmd(cmd: list[str]) -> str:
    """Return a redacted representation of a command (no key values)."""
    redacted: list[str] = []
    skip_next = False
    for part in cmd:
        if skip_next:
            skip_next = False
            # Replace key:secret with key:<redacted>
            if ":" in part:
                kid = part.split(":", 1)[0]
                redacted.append(f"{kid}:<redacted>")
            else:
                redacted.append("<redacted>")
            continue
        if part == "--key":
            redacted.append(part)
            skip_next = True
        else:
            redacted.append(part)
    return " ".join(redacted)


def _mp4decrypt_tmp_path(final: Path) -> Path:
    """Return a hidden temporary path while preserving the media suffix."""
    return final.with_name(f".{final.stem}.mp4decrypt.tmp{final.suffix}")


# ---------------------------------------------------------------------------
# mp4decrypt async runner
# ---------------------------------------------------------------------------


async def run_mp4decrypt(
    input_path: str | Path,
    output_path: str | Path,
    keys: list[ContentKey],
    *,
    overwrite: bool = False,
    timeout: float | None = None,
) -> Path:
    """Run ``mp4decrypt`` asynchronously and return the final output path.

    The function writes to a temporary file next to *output_path* and renames
    it atomically on success.  If the process fails, the partial file is
    cleaned up and never promoted to *output_path*.

    Args:
        input_path: Encrypted ``.mp4`` input file.
        output_path: Desired final output path.
        keys: Content keys to decrypt with (at least one).
        overwrite: Allow overwriting an existing *output_path*.
        timeout: Optional timeout in seconds.

    Returns:
        The resolved :class:`Path` of the decrypted file.

    Raises:
        DecryptError: on any failure; key material is never exposed.
    """
    cmd = build_mp4decrypt_command(input_path, output_path, keys, overwrite=overwrite)
    final = Path(output_path)
    if final.exists() and not overwrite:
        raise DecryptError(
            f"Output file already exists: {final}. Pass overwrite=True to replace it."
        )

    # Write to a temporary path next to the final destination.
    tmp_path = _mp4decrypt_tmp_path(final)
    _cleanup(tmp_path)

    # Ensure the command targets the temporary path.
    cmd[-1] = str(tmp_path)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        _cleanup(tmp_path)
        raise DecryptError(
            f"mp4decrypt timed out after {timeout}s.  Command: {_redact_cmd(cmd)}"
        ) from None
    except FileNotFoundError:
        _cleanup(tmp_path)
        raise DecryptError(
            "mp4decrypt not found on PATH.  Install Bento4 utilities."
        ) from None
    except Exception as exc:
        _cleanup(tmp_path)
        raise DecryptError(
            f"mp4decrypt failed to execute: {type(exc).__name__}.  "
            f"Command: {_redact_cmd(cmd)}"
        ) from exc

    if proc.returncode != 0:
        _cleanup(tmp_path)
        raise DecryptError(
            f"mp4decrypt exited with code {proc.returncode}.  "
            f"Command: {_redact_cmd(cmd)}"
        )

    if not tmp_path.is_file():
        raise DecryptError(
            "mp4decrypt reported success but the temporary output file is missing."
        )

    # Atomic rename — only on success.
    os.replace(tmp_path, final)
    return final


def _cleanup(path: Path) -> None:
    """Best-effort removal of a partial temporary file."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


async def run_ffmpeg_mux(
    input_paths: Sequence[str | Path],
    output_path: str | Path,
    *,
    overwrite: bool = False,
    timeout: float | None = None,
) -> Path:
    """Mux decrypted audio/video tracks into one MP4 without re-encoding."""
    inputs = [Path(path) for path in input_paths]
    if len(inputs) < 2:
        raise DecryptError("At least two decrypted tracks are required for muxing.")
    if any(not path.is_file() for path in inputs):
        raise DecryptError("A decrypted track required for muxing is missing.")

    final = Path(output_path)
    if final.exists() and not overwrite:
        raise DecryptError(f"Output file already exists: {final}.")
    tmp_path = final.with_suffix(final.suffix + ".ffmpeg.tmp")
    _cleanup(tmp_path)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for path in inputs:
        cmd.extend(["-i", str(path)])
    for index in range(len(inputs)):
        cmd.extend(["-map", str(index)])
    cmd.extend(["-c", "copy", "-f", "mp4", str(tmp_path)])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        _cleanup(tmp_path)
        raise DecryptError(f"ffmpeg mux timed out after {timeout}s.") from None
    except FileNotFoundError:
        _cleanup(tmp_path)
        raise DecryptError("ffmpeg not found on PATH. Install FFmpeg.") from None
    except Exception as exc:
        _cleanup(tmp_path)
        raise DecryptError(f"ffmpeg failed to execute: {type(exc).__name__}") from exc

    if proc.returncode != 0:
        _cleanup(tmp_path)
        raise DecryptError(f"ffmpeg mux exited with code {proc.returncode}.")
    if not tmp_path.is_file():
        raise DecryptError("ffmpeg reported success but the temporary MP4 is missing.")

    os.replace(tmp_path, final)
    return final


async def probe_stream_types(path: str | Path) -> set[str]:
    """Read stream types from a media file using ffprobe."""
    media_path = Path(path)
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
    except FileNotFoundError:
        raise DecryptError("ffprobe not found on PATH. Install FFmpeg.") from None
    except Exception as exc:
        raise DecryptError(f"ffprobe failed to execute: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise DecryptError(f"ffprobe failed for media file: {media_path}")
    return {line.strip() for line in stdout.decode(errors="replace").splitlines() if line.strip()}


async def validate_mp4_streams(path: str | Path, required: set[str]) -> None:
    """Ensure the final MP4 contains every stream type present in its inputs."""
    actual = await probe_stream_types(path)
    missing = sorted(required - actual)
    if missing:
        raise DecryptError(
            f"Final MP4 is missing required stream(s): {', '.join(missing)}."
        )
