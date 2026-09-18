"""Vistas de los incidentes de conducta para el usuario y para la cola de revisión (dev).

Las reglas (apelar, resolver, recalcular la restricción) están en `conduct_service`; aquí
solo se consulta y se da forma.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.models.story import ConductIncident, Story
from app.models.user import User
from app.schemas.conduct import (
    DevIncidentOut, DevIncidentPage, IncidentReviewOut, IncidentStoryOut, IncidentUserOut, MyIncidentOut,
)
from app.services import conduct_service, story_service

QueueFilter = Literal["pendientes", "sin_resolver", "recientes", "resueltos", "todos"]


def _book_title(db: DbSession, story: Story | None) -> str:
    if story is None:
        return "Libro no disponible"
    profile = story_service.story_profile(db, story)
    return profile.nombre if profile else story.character_id


def my_incident_out(db: DbSession, incident: ConductIncident) -> MyIncidentOut:
    return MyIncidentOut(
        id=incident.id,
        storyId=incident.story_id,
        bookTitle=_book_title(db, db.get(Story, incident.story_id)),
        level=incident.level,
        createdAt=incident.created_at,
        appealStatus=conduct_service.appeal_status(incident),
        appealText=incident.appeal_text,
        appealedAt=incident.appealed_at,
        reviewedAt=incident.reviewed_at,
        reviewNote=incident.review_note,
        counts=incident.review_status != conduct_service.REVIEW_ACCEPTED,
    )


def my_incidents(db: DbSession, user: User) -> list[MyIncidentOut]:
    return [my_incident_out(db, i) for i in conduct_service.own_incidents(db, user)]


def dev_incident_out(db: DbSession, incident: ConductIncident, viewer: User, *, detail: bool = False) -> DevIncidentOut:
    owner = db.get(User, incident.user_id)
    story = db.get(Story, incident.story_id)
    excerpt = conduct_service.visible_excerpt(incident)
    review = None
    if incident.review_status is not None and incident.reviewed_at is not None:
        review = IncidentReviewOut(
            status=incident.review_status,
            reviewedAt=incident.reviewed_at,
            reviewedBy=incident.reviewed_by_handle,
            note=incident.review_note,
        )
    return DevIncidentOut(
        id=incident.id,
        createdAt=incident.created_at,
        level=incident.level,
        rule=incident.rule,
        user=IncidentUserOut(
            id=owner.id,
            handle=owner.handle,
            username=owner.username,
            restrictedUntil=conduct_service.restricted_until(owner),
        )
        if owner
        else None,
        story=IncidentStoryOut(
            id=story.id, characterId=story.character_id, bookTitle=_book_title(db, story), status=story.status
        )
        if story
        else None,
        appealStatus=conduct_service.appeal_status(incident),
        appealText=incident.appeal_text,
        appealedAt=incident.appealed_at,
        review=review,
        hasExcerpt=excerpt is not None,
        excerpt=excerpt if detail else None,
        canReview=incident.review_status is None and conduct_service.can_review(viewer, incident),
    )


def queue(
    db: DbSession,
    viewer: User,
    *,
    filter: QueueFilter = "pendientes",
    level: str | None = None,
    rule: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DevIncidentPage:
    where = []
    if filter == "pendientes":
        where += [ConductIncident.appealed_at.is_not(None), ConductIncident.review_status.is_(None)]
    elif filter == "sin_resolver":
        where.append(ConductIncident.review_status.is_(None))
    elif filter == "recientes":
        where.append(ConductIncident.created_at >= conduct_service.now() - timedelta(days=settings.CONDUCT_WINDOW_DAYS))
    elif filter == "resueltos":
        where.append(ConductIncident.review_status.is_not(None))
    if level:
        where.append(ConductIncident.level == level)
    if rule:
        # Prefijo: "llm:" trae todas las del moderador LLM.
        where.append(ConductIncident.rule.startswith(rule, autoescape=True))
    total = db.scalar(select(func.count()).select_from(ConductIncident).where(*where)) or 0
    # Las apeladas primero por antigüedad de la apelación: la que más espera, arriba.
    order = (
        (ConductIncident.appealed_at.asc(), ConductIncident.id.asc())
        if filter == "pendientes"
        else (ConductIncident.created_at.desc(), ConductIncident.id.desc())
    )
    rows = db.scalars(select(ConductIncident).where(*where).order_by(*order).limit(limit).offset(offset))
    return DevIncidentPage(items=[dev_incident_out(db, i, viewer) for i in rows], total=total)
