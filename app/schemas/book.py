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
    # null = Psique (predefinido).
    author: AuthorOut | None
    isMine: bool
    isPublic: bool
    chapterCount: int
    freeFirstRead: bool
    readCost: int
    publishedAt: datetime | None
    createdAt: datetime | None
    stats: BookStatsOut
    viewer: BookViewerOut


class HistoryItemOut(BaseModel):
    storyId: str
    startedAt: datetime | None
    archivedAt: datetime | None
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
    author: AuthorOut | None
    readers: int


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
