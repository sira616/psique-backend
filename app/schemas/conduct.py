"""Incidentes de conducta: lo que ve el usuario (sin regla ni extracto) y lo que ve un dev."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AppealStatus = Literal["pendiente", "aceptada", "rechazada"]


class AppealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=500)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=300)


class MyIncidentOut(BaseModel):
    id: int
    storyId: str
    bookTitle: str
    level: str
    createdAt: datetime
    # null = sin apelar.
    appealStatus: AppealStatus | None
    appealText: str | None
    appealedAt: datetime | None
    reviewedAt: datetime | None
    reviewNote: str | None
    # Si todavía cuenta para la restricción (no aceptado en revisión).
    counts: bool


class IncidentUserOut(BaseModel):
    id: str
    handle: str
    username: str
    restrictedUntil: datetime | None


class IncidentStoryOut(BaseModel):
    id: str
    characterId: str
    bookTitle: str
    status: str


class IncidentReviewOut(BaseModel):
    status: Literal["aceptada", "rechazada"]
    reviewedAt: datetime
    reviewedBy: str | None
    note: str | None


class DevIncidentOut(BaseModel):
    id: int
    createdAt: datetime
    level: str
    rule: str | None
    user: IncidentUserOut | None
    story: IncidentStoryOut | None
    appealStatus: AppealStatus | None
    appealText: str | None
    appealedAt: datetime | None
    review: IncidentReviewOut | None
    hasExcerpt: bool
    # Solo en el detalle: en el listado va siempre null.
    excerpt: str | None = None
    # En el detalle: si quien consulta puede resolverlo (no es suyo, o entorno de desarrollo).
    canReview: bool


class DevIncidentPage(BaseModel):
    items: list[DevIncidentOut]
    total: int


class ResolveOut(BaseModel):
    incident: DevIncidentOut
    storyReopened: bool
