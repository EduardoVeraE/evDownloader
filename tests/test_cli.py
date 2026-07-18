from typer.testing import CliRunner

from evdownloader import __version__
from evdownloader.cli import app

runner = CliRunner()


def test_version_option_shows_installed_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"evdownloader {__version__}"
