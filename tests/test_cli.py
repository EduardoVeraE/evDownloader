from unittest.mock import AsyncMock, patch

import pytest
from typer.core import TyperOption
from typer.main import get_command
from typer.testing import CliRunner

from evdownloader import __version__, cli

runner = CliRunner()


@pytest.mark.parametrize(("extra_args", "expected"), [([], False), (["--cache"], True)])
def test_download_cache_is_explicit_opt_in(extra_args: list[str], expected: bool) -> None:
    download_course = AsyncMock()

    with (
        patch("evdownloader.cli.ensure_dirs"),
        patch("evdownloader.service.download_course", download_course),
    ):
        result = runner.invoke(cli.app, ["download", "https://example.test/course", *extra_args])

    assert result.exit_code == 0
    assert download_course.await_args.kwargs["use_cache"] is expected


def test_download_help_exposes_cache_opt_in_only() -> None:
    download = get_command(cli.app).commands["download"]
    options = [param for param in download.params if isinstance(param, TyperOption)]
    cache = next(option for option in options if option.name == "cache")

    assert cache.opts == ["--cache"]
    assert cache.default is False
    assert "--no-cache" not in {
        flag for option in options for flag in (*option.opts, *option.secondary_opts)
    }


def test_version_option_shows_installed_version() -> None:
    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"evdownloader {__version__}"


def test_update_reports_when_already_current(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_latest_published_version", lambda: __version__)

    result = runner.invoke(cli.app, ["--update"])

    assert result.exit_code == 0
    assert "Ya estás actualizado" in result.stdout


def test_update_runs_installer_for_newer_version(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(cli, "_latest_published_version", lambda: "99.0.0")
    monkeypatch.setattr(cli, "_upgrade_package", lambda: calls.append(True) or 0)

    result = runner.invoke(cli.app, ["--update"])

    assert result.exit_code == 0
    assert calls == [True]
    assert "Actualizando evDownloader" in result.stdout
