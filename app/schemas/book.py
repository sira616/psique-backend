from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from app.schemas.profile import AuthorOut

REVIEW_TEXT_MAX = 1000


class BookStatsOut(BaseModel):
    readers: int
    ratingAverage: float | None
    reviewCount: int




class BookProgressOut(BaseModel):
    phase: str
    phaseLabel: str
    phaseIndex: int
    phaseCount: int
    affinity: int
    chapterLocked: bool


class PrimaryActionOut(BaseModel):
    kind: Literal["leer", "continuar"]
    cost: int


class ReviewOut(BaseModel):
    id: int
    rating: int
    text: str | None
    author: AuthorOut
    isMine: bool
    createdAt: datetime | None
    updatedAt: datetime | None


class CustomStoryStatsOut(BaseModel):
    """Lo que ve el autor de su propia historia (`GET /api/custom-stories/{id}/stats`).

    Vive aquí y no en `app.schemas.custom_story` para no montar un ciclo de imports:
    reutiliza `ReviewOut`, y este módulo ya importa de `profile`, que importa de
    `custom_story`.
    """

    # Cuentas distintas que han jugado la historia, contando las partidas ya terminadas.
    readers: int
    # Partidas sin archivar ni cerrar ahora mismo (el mismo criterio que `active_story`).
    activeStories: int
    ratingAverage: float | None
    reviewCount: int
    recentReviews: list[ReviewOut]


class ReviewPageOut(BaseModel):
    items: list[ReviewOut]
    limit: int
    offset: int
    nextOffset: int | None


class BookViewerOut(BaseModel):
    status: Literal["sin_empezar", "leyendo", "leido"]
    activeStoryId: str | None
    progress: BookProgressOut | None
    primaryAction: PrimaryActionOut
    canReread: bool
    rereadCost: int
    canReview: bool
    myReview: ReviewOut | None
    # Libro +18 y cuenta sin confirmar: leer, continuar y releer darán 403 `adult_required`.
    adultRequired: bool


class BookOut(BaseModel):
    """Página de un libro. Nunca lleva premisa ni perfil: en concepto son la trama que se
    descubre jugando (ver `custom_story_service.card_out`)."""

    id: str
    origin: Literal["psique", "propia"]
    mode: Literal["definida", "concepto"] | None
    title: str
    hook: str
    characterName: str | None
    tone: str | None
    # Solo las historias propias tienen portada; los predefinidos, siempre null.
    coverUrl: str | None = None
    # null = Psique (predefinido).
    author: AuthorOut | None
    isMine: bool
    isPublic: bool
    chapterCount: int
    freeFirstRead: bool
    adult: bool
    readCost: int
    publishedAt: datetime | None
    createdAt: datetime | None
    stats: BookStatsOut
    viewer: BookViewerOut


class HistoryItemOut(BaseModel):
    storyId: str
    status: Literal["archivada", "cerrada"]
    startedAt: datetime | None
    archivedAt: datetime | None
    closedAt: datetime | None
    phase: str
    phaseLabel: str
    phaseIndex: int
    phaseCount: int
    affinity: int


class BookCardOut(BaseModel):
    id: str
    origin: Literal["psique", "propia"]
    mode: Literal["definida", "concepto"] | None
    title: str
    hook: str
    tone: str | None
    coverUrl: str | None = None
    author: AuthorOut | None
    readers: int
    adult: bool


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: int = Field(ge=1, le=5)
    text: str | None = None

    @field_validator("text")
    @classmethod
    def texto(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if len(value) > REVIEW_TEXT_MAX:
            raise PydanticCustomError("too_long", f"La reseña admite como mucho {REVIEW_TEXT_MAX} caracteres.")
        return value or None
