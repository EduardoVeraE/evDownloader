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
        overwrite: If *True*, pass ``--force-overwrite``.

    Returns:
        Argument list suitable for :func:`asyncio.create_subprocess_exec`.

    Raises:
        DecryptError: if *keys* is empty.
    """
    if not keys:
        raise DecryptError("At least one content key is required for decryption.")

    cmd: list[str] = ["mp4decrypt"]
    if overwrite:
        cmd.append("--force-overwrite")
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
    tmp_path = final.with_suffix(final.suffix + ".mp4decrypt.tmp")
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
