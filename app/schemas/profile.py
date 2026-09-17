import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_core import PydanticCustomError

from app.schemas.custom_story import DefinitionOut

HANDLE_PATTERN = re.compile(r"^[a-z0-9_]{3,30}$")
HANDLE_HELP = "El handle va de 3 a 30 caracteres: letras minúsculas, números o guion bajo."
BIO_MAX = 280
LINK_MAX = 200
DISPLAY_NAME_MAX = 64


# --- Tarjetas (Explorar y estantería "Publicadas") ---------------------------------------


class AuthorOut(BaseModel):
    displayName: str
    handle: str
    avatarUrl: str | None


class StoryCardOut(BaseModel):
    """Historia publicada vista por cualquier cuenta. Ver `custom_story_service.card_out`
    para qué campos salen en cada modo."""

    id: str
    characterId: str
    mode: Literal["definida", "concepto"]
    title: str
    hook: str
    tone: str | None
    # Solo en modo definida: lo que escribió el autor. En concepto, siempre null.
    definition: DefinitionOut | None
    author: AuthorOut
    isMine: bool
    publishedAt: datetime | None


class ExplorePageOut(BaseModel):
    items: list[StoryCardOut]
    limit: int
    offset: int
    # null cuando no hay más páginas.
    nextOffset: int | None


# --- Perfil ------------------------------------------------------------------------------


class ProgressOut(BaseModel):
    phase: str
    phaseLabel: str
    phaseIndex: int
    phaseCount: int


class ReadingItemOut(BaseModel):
    """Una partida en curso, sin mensajes, hechos ni afinidad: solo qué y por dónde va."""

    characterId: str
    origin: Literal["psique", "propia"]
    mode: Literal["definida", "concepto"] | None
    title: str
    # null en modo concepto: el nombre del personaje es parte de lo que se descubre jugando.
    characterName: str | None
    progress: ProgressOut
    updatedAt: datetime | None


class PublishedShelfOut(BaseModel):
    # null si quien mira no es el dueño y el dueño la ha ocultado.
    items: list[StoryCardOut] | None
    # Si otras cuentas la ven. El dueño recibe `items` siempre, y esto le dice si están ocultos.
    visibleToOthers: bool


class ReadingShelfOut(BaseModel):
    items: list[ReadingItemOut] | None
    visibleToOthers: bool


class ShelvesOut(BaseModel):
    published: PublishedShelfOut
    reading: ReadingShelfOut


class ProfileOut(BaseModel):
    handle: str
    displayName: str
    bio: str | None
    link: str | None
    avatarUrl: str | None
    bannerUrl: str | None
    joinedAt: datetime | None
    isOwner: bool
    shelves: ShelvesOut


class MyProfileOut(BaseModel):
    """Lo editable del perfil propio, con los mismos nombres que acepta el PATCH."""

    handle: str
    displayName: str
    bio: str | None
    link: str | None
    avatarUrl: str | None
    bannerUrl: str | None
    showPublished: bool
    showReading: bool


class ProfilePatchIn(BaseModel):
    """Campo ausente = no cambia. `bio` y `link` a null o "" se borran; el resto no se
    puede vaciar.

    Los validadores lanzan `PydanticCustomError` y no `ValueError` para que `msg` llegue
    en español y sin el prefijo "Value error, " que el frontend tendría que recortar.
    """

    model_config = ConfigDict(extra="forbid")

    displayName: str | None = None
    handle: str | None = None
    bio: str | None = None
    link: str | None = None
    showPublished: bool | None = None
    showReading: bool | None = None

    @field_validator("displayName", "handle", "showPublished", "showReading")
    @classmethod
    def no_nulo(cls, value):
        # Solo corre si el campo vino en el cuerpo: los valores por defecto no se validan.
        if value is None:
            raise PydanticCustomError("required", "Este campo no se puede dejar vacío.")
        return value

    @field_validator("displayName")
    @classmethod
    def nombre_visible(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise PydanticCustomError("required", "Este campo no se puede dejar vacío.")
        if len(value) > DISPLAY_NAME_MAX:
            raise PydanticCustomError("too_long", f"Como mucho {DISPLAY_NAME_MAX} caracteres.")
        return value

    @field_validator("handle")
    @classmethod
    def formato_handle(cls, value: str) -> str:
        value = value.strip().lower()
        if not HANDLE_PATTERN.match(value):
            raise PydanticCustomError("handle_format", HANDLE_HELP)
        return value

    @field_validator("bio")
    @classmethod
    def longitud_bio(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if len(value) > BIO_MAX:
            raise PydanticCustomError("too_long", f"La bio admite como mucho {BIO_MAX} caracteres.")
        return value or None

    @field_validator("link")
    @classmethod
    def enlace_http(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if not value:
            return None
        invalid = PydanticCustomError(
            "link_format", "El enlace tiene que ser una dirección web completa (http:// o https://)."
        )
        if len(value) > LINK_MAX:
            raise PydanticCustomError("too_long", f"El enlace admite como mucho {LINK_MAX} caracteres.")
        if any(c.isspace() or ord(c) < 32 for c in value):
            raise invalid
        try:
            parts = urlsplit(value)
            host = parts.hostname
        except ValueError:
            raise invalid
        # Sin usuario@: "https://banco.es@otra.web" parece un sitio y lleva a otro.
        if parts.scheme.lower() not in ("http", "https") or not host or "." not in host or "@" in parts.netloc:
            raise invalid
        return value
