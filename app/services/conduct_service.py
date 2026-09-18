"""Consecuencias de la política de contenido sobre partidas y cuentas.

Qué nivel tiene un mensaje y qué acción toca está en `app.story.content_policy`; aquí se
aplica: cerrar la partida, registrar el incidente y, si hay reincidencia, restringir la
cuenta durante `CONDUCT_RESTRICTION_DAYS`.

Tras un cierre se puede releer el libro (cuesta una relectura normal) salvo con la cuenta
restringida: el cierre castiga esa partida, la restricción castiga la reincidencia.

Revisión y apelación: el usuario puede apelar cada incidente una vez; un dev lo acepta
(reabre la partida, lo saca del cómputo y recalcula la restricción) o lo rechaza.

Privacidad del extracto: para revisar hace falta ver qué se escribió, así que el incidente
guarda los primeros `CONDUCT_EXCERPT_CHARS` caracteres del mensaje. Solo lo ven los devs
(y el usuario, en su export de datos). Se borra al resolver el incidente y deja de
mostrarse a los `CONDUCT_EXCERPT_DAYS`; `scripts.cleanup` lo borra de la base. Con el
borrado de cuenta se va con el incidente.

Un dev no resuelve sus propios incidentes salvo con `ENVIRONMENT=development`, donde es
la única forma de probar la cola en local.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
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


REVIEW_ACCEPTED = "aceptada"
REVIEW_REJECTED = "rechazada"
APPEAL_PENDING = "pendiente"


def close_story(
    db: DbSession, user: User, story: Story, classification: Classification, excerpt: str | None = None
) -> datetime | None:
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
                excerpt=(excerpt or "").strip()[: settings.CONDUCT_EXCERPT_CHARS] or None,
            )
        )
        db.flush()
        window_start = closed_at - timedelta(days=settings.CONDUCT_WINDOW_DAYS)
        recent = db.scalar(
            select(func.count())
            .select_from(ConductIncident)
            .where(
                ConductIncident.user_id == user.id,
                ConductIncident.created_at >= window_start,
                _counts(),
            )
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
    if not _reopen(db, story):
        return False
    db.commit()
    db.refresh(story)
    return True


def lift_restriction(db: DbSession, user: User) -> User:
    user.restricted_until = None
    db.commit()
    db.refresh(user)
    return user


# --- Revisión y apelación ------------------------------------------------------------

class ReviewError(Exception):
    """Fallo esperado al apelar o resolver, con código para el cliente."""

    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _counts():
    """Incidentes que cuentan para la restricción: todos menos los aceptados en revisión."""
    return or_(ConductIncident.review_status.is_(None), ConductIncident.review_status != REVIEW_ACCEPTED)


def _reopen(db: DbSession, story: Story) -> bool:
    """Reabre sin commit. False si no está cerrada o ya hay otra activa del mismo libro."""
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
    return True


def appeal_status(incident: ConductIncident) -> str | None:
    """None sin apelar; si no, pendiente/aceptada/rechazada. Un incidente resuelto sin
    apelación también enseña su resolución."""
    if incident.review_status is not None:
        return incident.review_status
    return APPEAL_PENDING if incident.appealed_at is not None else None


def visible_excerpt(incident: ConductIncident, at: datetime | None = None) -> str | None:
    """El extracto solo mientras no haya caducado, aunque `cleanup` aún no lo haya borrado."""
    if incident.excerpt is None:
        return None
    if incident.created_at <= (at or now()) - timedelta(days=settings.CONDUCT_EXCERPT_DAYS):
        return None
    return incident.excerpt


def own_incidents(db: DbSession, user: User) -> list[ConductIncident]:
    return list(
        db.scalars(
            select(ConductIncident)
            .where(ConductIncident.user_id == user.id)
            .order_by(ConductIncident.created_at.desc(), ConductIncident.id.desc())
        )
    )


def appeal(db: DbSession, user: User, incident_id: int, text: str | None) -> ConductIncident:
    """Una apelación por incidente propio. El texto ya viene filtrado por la ruta."""
    incident = db.get(ConductIncident, incident_id)
    if incident is None or incident.user_id != user.id:
        raise ReviewError("not_found", "Ese incidente no existe.", 404)
    if incident.review_status is not None:
        raise ReviewError("already_resolved", "Este incidente ya está revisado.")
    # UPDATE condicionado: dos envíos a la vez no guardan dos apelaciones.
    result = db.execute(
        update(ConductIncident)
        .where(ConductIncident.id == incident.id, ConductIncident.appealed_at.is_(None))
        .values(appealed_at=now(), appeal_text=(text or "").strip() or None)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise ReviewError("already_appealed", "Ya apelaste este incidente.")
    db.commit()
    db.refresh(incident)
    return incident


def can_review(dev: User, incident: ConductIncident) -> bool:
    return incident.user_id != dev.id or settings.ENVIRONMENT.strip().lower() == "development"


def recompute_restriction(db: DbSession, user: User) -> datetime | None:
    """Recalcula `restricted_until` con los incidentes que siguen contando.

    Solo acorta o levanta: una restricción más larga por otro motivo no se alarga al
    revisar. Recorre cada incidente como si fuera el último de su ventana, igual que
    `close_story` al crearlo.
    """
    rows = list(
        db.scalars(
            select(ConductIncident.created_at)
            .where(ConductIncident.user_id == user.id, _counts())
            .order_by(ConductIncident.created_at)
        )
    )
    window = timedelta(days=settings.CONDUCT_WINDOW_DAYS)
    computed: datetime | None = None
    for i, created in enumerate(rows):
        in_window = sum(1 for other in rows[: i + 1] if other >= created - window)
        if in_window >= settings.CONDUCT_CLOSURES_LIMIT:
            computed = created + timedelta(days=settings.CONDUCT_RESTRICTION_DAYS)
    if computed is not None and computed <= now():
        computed = None
    if user.restricted_until is not None and (computed is None or computed < user.restricted_until):
        user.restricted_until = computed
    return restricted_until(user)


def resolve(
    db: DbSession, dev: User, incident_id: int, accept: bool, note: str | None
) -> tuple[ConductIncident, bool]:
    """Acepta o rechaza un incidente. Devuelve el incidente y si se reabrió la partida.

    Aceptar reabre la partida si puede (no, si ya hay otra activa del mismo libro) y
    recalcula la restricción. Todo, auditoría incluida, en la misma transacción.
    """
    incident = db.get(ConductIncident, incident_id)
    if incident is None:
        raise ReviewError("not_found", "Ese incidente no existe.", 404)
    if not can_review(dev, incident):
        raise ReviewError("own_incident", "No puedes revisar un incidente tuyo.", 403)
    result = db.execute(
        update(ConductIncident)
        .where(ConductIncident.id == incident.id, ConductIncident.review_status.is_(None))
        .values(
            review_status=REVIEW_ACCEPTED if accept else REVIEW_REJECTED,
            reviewed_at=now(),
            reviewed_by_id=dev.id,
            reviewed_by_handle=dev.handle,
            review_note=(note or "").strip()[:300] or None,
            # Resuelto, ya no hace falta para nada.
            excerpt=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise ReviewError("already_resolved", "Este incidente ya está revisado.")
    reopened = False
    if accept:
        story = db.get(Story, incident.story_id)
        reopened = story is not None and _reopen(db, story)
        owner = db.get(User, incident.user_id)
        if owner is not None:
            db.flush()
            recompute_restriction(db, owner)
    db.commit()
    db.refresh(incident)
    return incident, reopened


def purge_expired_excerpts(db: DbSession, at: datetime | None = None, *, apply: bool = True) -> int:
    """Borra (o cuenta, con apply=False) los extractos caducados. Lo usa `scripts.cleanup`."""
    cutoff = (at or now()) - timedelta(days=settings.CONDUCT_EXCERPT_DAYS)
    where = (ConductIncident.excerpt.is_not(None), ConductIncident.created_at <= cutoff)
    if not apply:
        return db.scalar(select(func.count()).select_from(ConductIncident).where(*where)) or 0
    result = db.execute(
        update(ConductIncident).where(*where).values(excerpt=None).execution_options(synchronize_session=False)
    )
    return result.rowcount or 0
