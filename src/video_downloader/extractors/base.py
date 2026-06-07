"""Interfaz común para extractores de plataforma."""

from __future__ import annotations

from abc import ABC, abstractmethod

from playwright.async_api import BrowserContext

from ..models import Course, Unit, VideoSource


class Extractor(ABC):
    """Contrato que debe cumplir cada extractor de plataforma.

    El flujo del núcleo es:
        1. ``list_course(ctx, url)`` -> estructura completa del curso.
        2. para cada unidad de video: ``resolve_video(ctx, unit)`` -> fuente
           lista para entregar al downloader.
    """

    #: Nombre legible de la plataforma.
    name: str = "base"

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
