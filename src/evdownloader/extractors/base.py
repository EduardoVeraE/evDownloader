"""Interfaz común para extractores de plataforma."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from playwright.async_api import BrowserContext

from ..models import Course, Unit, UnitExtras, VideoSource

if TYPE_CHECKING:
    from ..config import Settings


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

    #: Si el extractor necesita un contexto de navegador (Playwright) para
    #: operar. Los extractores que delegan en yt-dlp (p. ej. Udemy) lo ponen en
    #: ``False`` para que el núcleo NO abra Playwright.
    needs_browser: bool = True

    #: URL donde el usuario inicia sesión manualmente.
    login_url: str = ""
    #: URL a la que navegar para verificar que la sesión sigue activa.
    home_url: str = ""
    #: Selector que solo aparece cuando la sesión está autenticada (p. ej. el
    #: avatar o menú de usuario). Puede listar varias alternativas separadas por
    #: coma. Lo usan ``session.login`` y ``session.is_logged_in``.
    auth_ready_selector: str = ""

    #: Provider-owned hosts allowed for direct resource downloads. Empty means
    #: resources fail closed rather than trusting extractor-provided URLs.
    resource_host_suffixes: tuple[str, ...] = ()

    def configure(self, settings: Settings) -> None:  # noqa: B027
        """Recibe los ajustes de la ejecución antes de listar/descargar.

        El núcleo la invoca tras construir el extractor. La implementación por
        defecto no hace nada; los extractores que necesitan opciones de runtime
        (p. ej. Udemy con ``cookies_from_browser``) la sobrescriben.
        """

    async def verify_session(self, cookies: Sequence[Mapping[str, Any]]) -> bool | None:
        """Confirma si ``cookies`` corresponden a una sesión autenticada.

        Devuelve ``True``/``False`` cuando la plataforma puede comprobarlo con
        una petición ligera, o ``None`` cuando no hay forma de verificar y se
        debe confiar en la mera presencia de la cookie de sesión.

        La implementación por defecto no verifica (``None``). Los extractores que
        emiten cookies de invitado antes del login (p. ej. Udemy) la sobrescriben
        para evitar persistir una sesión anónima.
        """
        return None

    @staticmethod
    @abstractmethod
    def supports(url: str) -> bool:
        """Indica si este extractor puede manejar la URL dada."""

    @abstractmethod
    async def list_course(self, ctx: BrowserContext | None, url: str) -> Course:
        """Extrae la estructura del curso (capítulos y unidades)."""

    @abstractmethod
    async def resolve_video(self, ctx: BrowserContext | None, unit: Unit) -> VideoSource | None:
        """Resuelve la fuente de video de una unidad."""

    async def resolve_extras(
        self, ctx: BrowserContext | None, unit: Unit, *, capture_page: bool = False
    ) -> UnitExtras:
        """Resuelve el material complementario de una unidad.

        Devuelve resumen, recursos adjuntos y (si ``capture_page``) un snapshot
        MHTML de la página. La implementación por defecto no aporta extras.
        """
        return UnitExtras()
