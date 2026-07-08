"""Interfaz común para extractores de plataforma."""

from __future__ import annotations

from abc import ABC, abstractmethod

from playwright.async_api import BrowserContext

from ..models import Course, Unit, UnitExtras, VideoSource


class Extractor(ABC):
    """Contrato que debe cumplir cada extractor de plataforma.

    El flujo del núcleo es:
        1. ``list_course(ctx, url)`` -> estructura completa del curso.
        2. para cada unidad de video: ``resolve_video(ctx, unit)`` -> fuente
           lista para entregar al downloader.
        3. opcionalmente, ``resolve_extras(ctx, unit)`` -> resumen, recursos
           adjuntos y/o snapshot de la página.
    """

    #: Nombre legible de la plataforma. Se usa también como clave de sesión
    #: (archivo de cookies ``session-{name}.json``).
    name: str = "base"

    #: URL donde el usuario inicia sesión manualmente.
    login_url: str = ""
    #: URL a la que navegar para verificar que la sesión sigue activa.
    home_url: str = ""
    #: Selector que solo aparece cuando la sesión está autenticada (p. ej. el
    #: avatar o menú de usuario). Puede listar varias alternativas separadas por
    #: coma. Lo usan ``session.login`` y ``session.is_logged_in``.
    auth_ready_selector: str = ""

    @staticmethod
    @abstractmethod
    def supports(url: str) -> bool:
        """Indica si este extractor puede manejar la URL dada."""

    @abstractmethod
    async def list_course(self, ctx: BrowserContext, url: str) -> Course:
        """Extrae la estructura del curso (capítulos y unidades)."""

    @abstractmethod
    async def resolve_video(self, ctx: BrowserContext, unit: Unit) -> VideoSource | None:
        """Resuelve la fuente de video de una unidad navegando a su página."""

    async def resolve_extras(
        self, ctx: BrowserContext, unit: Unit, *, capture_page: bool = False
    ) -> UnitExtras:
        """Resuelve el material complementario de una unidad.

        Devuelve resumen, recursos adjuntos y (si ``capture_page``) un snapshot
        MHTML de la página. La implementación por defecto no aporta extras.
        """
        return UnitExtras()
