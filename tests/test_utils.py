"""Tests de utilidades de nombres y selección de formato."""

from __future__ import annotations

from video_downloader.downloaders.base import Downloader
from video_downloader.utils import numbered, slugify


def test_slugify_translitera_acentos() -> None:
    assert slugify("Introducción básica") == "Introduccion basica"


def test_slugify_colapsa_espacios() -> None:
    assert slugify("a    b") == "a b"


def test_slugify_quita_caracteres_invalidos() -> None:
    assert "/" not in slugify("a/b\\c:d*e?")
    assert "?" not in slugify("a?b")


def test_slugify_vacio_da_untitled() -> None:
    assert slugify("///") == "untitled"


def test_numbered_prefija_dos_digitos() -> None:
    assert numbered(1, "Intro").startswith("01-")
    assert numbered(12, "Cierre").startswith("12-")


def test_format_selector_con_calidad() -> None:
    sel = Downloader._format_selector("1080")
    assert "height<=1080" in sel


def test_format_selector_sin_calidad() -> None:
    assert Downloader._format_selector(None) == "bv*+ba/b"
