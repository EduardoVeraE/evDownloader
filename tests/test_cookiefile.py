"""Tests del cookiefile Netscape para yt-dlp (tarea dl9)."""

from __future__ import annotations

from evdownloader.downloaders.ytdlp import _SESSION_COOKIE_EXPIRY, render_netscape
from evdownloader.models import Cookie


def test_cabecera_netscape() -> None:
    out = render_netscape([])
    assert out.startswith("# Netscape HTTP Cookie File")


def test_linea_con_campos_separados_por_tabs() -> None:
    c = Cookie(name="sid", value="abc", domain=".platzi.com", path="/", secure=True, expires=123456)
    line = render_netscape([c]).splitlines()[1]
    assert line.split("\t") == [".platzi.com", "TRUE", "/", "TRUE", "123456", "sid", "abc"]


def test_cookie_de_sesion_usa_expiracion_lejana() -> None:
    # expires <= 0 (cookie de sesión) -> expiración lejana para que yt-dlp la conserve.
    c = Cookie(name="t", value="v", domain="platzi.com", expires=0)
    line = render_netscape([c]).splitlines()[1]
    assert line.split("\t")[4] == str(_SESSION_COOKIE_EXPIRY)


def test_flag_subdominios_segun_punto_inicial() -> None:
    sin_punto = render_netscape([Cookie(name="a", value="b", domain="platzi.com")]).splitlines()[1]
    con_punto = render_netscape([Cookie(name="a", value="b", domain=".platzi.com")]).splitlines()[1]
    assert sin_punto.split("\t")[1] == "FALSE"
    assert con_punto.split("\t")[1] == "TRUE"


def test_descarta_cookie_sin_dominio() -> None:
    # Sin dominio no se puede asociar a un host: se omite.
    out = render_netscape([Cookie(name="a", value="b", domain="")])
    assert out == "# Netscape HTTP Cookie File\n"
