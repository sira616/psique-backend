"""Consecuencias de la política de contenido sobre partidas y cuentas.

Qué nivel tiene un mensaje y qué acción toca está en `app.story.content_policy`; aquí se
aplica: cerrar la partida, registrar el incidente y, si hay reincidencia, restringir la
cuenta durante `CONDUCT_RESTRICTION_DAYS`.

Tras un cierre se puede releer el libro (cuesta una relectura normal) salvo con la cuenta
restringida: el cierre castiga esa partida, la restricción castiga la reincidencia.
Los incidentes no guardan el mensaje; revisar o recurrir un cierre queda pendiente.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.models.story import STORY_ACTIVE, STORY_CLOSED, ConductIncident, Story
from app.models.user import User
from app.story.content_policy import CLOSED_REASONS, Classification


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def restricted_until(user: User) -> datetime | None:
    """Fin de la restricción si sigue vigente; None si no hay o ya pasó."""
    if user.restricted_until is None or user.restricted_until <= now():
        return None
    return user.restricted_until


def adult_confirmed(user: User) -> bool:
    return user.adult_confirmed_at is not None


def confirm_adult(db: DbSession, user: User) -> User:
    if user.adult_confirmed_at is None:
        user.adult_confirmed_at = now()
        db.commit()
        db.refresh(user)
    return user


def revoke_adult(db: DbSession, user: User) -> User:
    user.adult_confirmed_at = None
    db.commit()
    db.refresh(user)
    return user


def close_story(db: DbSession, user: User, story: Story, classification: Classification) -> datetime | None:
    """Cierra la partida, registra el incidente y aplica la restricción si toca.

    Devuelve el fin de la restricción si la cuenta queda restringida (nueva o ya vigente).
    El UPDATE va condicionado a que siga activa: dos mensajes simultáneos no cuentan dos
    incidentes por la misma partida.
    """
    closed_at = now()
    reason = CLOSED_REASONS[classification.level]
    result = db.execute(
        update(Story)
        .where(Story.id == story.id, Story.status == STORY_ACTIVE)
        .values(status=STORY_CLOSED, closed_at=closed_at, closed_reason=reason)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        db.add(
            ConductIncident(
                user_id=user.id,
                story_id=story.id,
                level=classification.level.value,
                rule=(classification.rule or "")[:40] or None,
                created_at=closed_at,
            )
        )
        db.flush()
        window_start = closed_at - timedelta(days=settings.CONDUCT_WINDOW_DAYS)
        recent = db.scalar(
            select(func.count())
            .select_from(ConductIncident)
            .where(ConductIncident.user_id == user.id, ConductIncident.created_at >= window_start)
        )
        if recent >= settings.CONDUCT_CLOSURES_LIMIT:
            until = closed_at + timedelta(days=settings.CONDUCT_RESTRICTION_DAYS)
            # Nunca acorta una restricción que ya dura más.
            if user.restricted_until is None or user.restricted_until < until:
                user.restricted_until = until
    db.commit()
    db.refresh(story)
    db.refresh(user)
    return restricted_until(user)


def reopen_story(db: DbSession, story: Story) -> bool:
    """Solo dev: vuelve a abrir una partida cerrada. False si ya hay otra activa del libro."""
    if story.status != STORY_CLOSED:
        return False
    other = db.scalar(
        select(Story.id).where(
            Story.user_id == story.user_id,
            Story.character_id == story.character_id,
            Story.status == STORY_ACTIVE,
        )
    )
    if other is not None:
        return False
    story.status = STORY_ACTIVE
    story.closed_at = None
    story.closed_reason = None
    db.commit()
    db.refresh(story)
    return True


def lift_restriction(db: DbSession, user: User) -> User:
    user.restricted_until = None
    db.commit()
    db.refresh(user)
    return user
