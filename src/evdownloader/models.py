"""Modelos de datos (pydantic) para cursos, unidades y fuentes de video."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class UnitType(StrEnum):
    """Tipo de contenido de una unidad/lección."""

    VIDEO = "video"
    LECTURE = "lecture"
    QUIZ = "quiz"


class ResourceKind(StrEnum):
    """Naturaleza de un recurso adjunto a una clase."""

    FILE = "file"  # archivo descargable (alojado por la plataforma)
    LINK = "link"  # enlace externo (lectura recomendada, herramienta, etc.)


class Resource(BaseModel):
    """Recurso adjunto a una clase: archivo descargable o enlace externo."""

    title: str
    url: str
    kind: ResourceKind = ResourceKind.LINK


class UnitExtras(BaseModel):
    """Material complementario de una clase, resuelto al visitar su página.

    Se mantiene fuera de :class:`Unit` (y, por tanto, de la caché de estructura)
    porque ``page_mhtml`` puede ser voluminoso y el resumen/recursos se vuelven a
    leer en cada descarga.
    """

    summary_html: str | None = None
    resources: list[Resource] = Field(default_factory=list)
    # Snapshot MHTML de la página (se captura para lectures/quizzes sin video).
    page_mhtml: str | None = None


class Subtitle(BaseModel):
    """Pista de subtítulos asociada a un video."""

    lang: str = "es"
    url: str


class Cookie(BaseModel):
    """Cookie completa (campos necesarios para un cookiefile Netscape)."""

    name: str
    value: str
    domain: str = ""
    path: str = "/"
    secure: bool = False
    # Epoch en segundos; <= 0 indica cookie de sesión (sin expiración fija).
    expires: float = 0.0


class DrmInfo(BaseModel):
    """Metadata DRM detectada para un video.

    Almacena la información necesaria para futuras llamadas a la license server
    y descifrado.  El esquema se identifica con un string normalizado (p. ej.
    ``"widevine"``, ``"fairplay"``).
    """

    scheme: str
    license_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    token: str | None = None
    pssh: str | None = None
    key_id: str | None = None


class VideoSource(BaseModel):
    """Fuente de video resuelta, lista para entregar a un downloader.

    ``url`` puede ser el embed de Mediastream (``https://mdstrm.com/embed/{id}``)
    o directamente el master playlist ``.m3u8``. ``http_headers`` lleva los
    encabezados coherentes (User-Agent, Referer); ``cookies`` el estado de sesión
    como ``name -> value`` (para construir el header ``Cookie`` en rnet/FFmpeg) y
    ``cookie_jar`` las cookies completas (para generar un cookiefile en yt-dlp).
    """

    url: str
    is_embed: bool = False
    http_headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    cookie_jar: list[Cookie] = Field(default_factory=list)
    subtitles: list[Subtitle] = Field(default_factory=list)
    # Si el downloader (yt-dlp) debe extraer los subtítulos junto con el video.
    # Lo usan los extractores que delegan la resolución en yt-dlp (Udemy); los
    # que traen las URLs de subtítulos aparte (Platzi) lo dejan en False.
    write_subs: bool = False
    # DRM: se rellena cuando el extractor detecta contenido protegido.
    drm: DrmInfo | None = None


class Unit(BaseModel):
    """Una lección dentro de un capítulo."""

    title: str
    url: str
    type: UnitType = UnitType.VIDEO
    index: int = 0
    # Se rellena al resolver el video (interceptación de red).
    video: VideoSource | None = None


class Chapter(BaseModel):
    """Agrupación de unidades (módulo del curso)."""

    title: str
    index: int = 0
    units: list[Unit] = Field(default_factory=list)


class Course(BaseModel):
    """Estructura completa de un curso."""

    title: str
    url: str
    chapters: list[Chapter] = Field(default_factory=list)
