from typer.testing import CliRunner

from evdownloader import __version__, cli

runner = CliRunner()


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
