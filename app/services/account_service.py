"""Exportar y borrar la cuenta (RGPD: acceso, portabilidad y supresión).

Decisiones del borrado:
- Partidas, mensajes, hechos, eventos, reseñas, tarjetas, incidentes, cupo de turnos y
  refresh tokens de la cuenta se borran.
- Movimientos de óbolos: se borran. Son saldo de juego sin dinero real detrás, así que no
  hay obligación contable que justifique conservarlos.
- Historias propias que nadie más jugó: se borran, con las reseñas que tuvieran.
- Historias propias con partidas de otras cuentas: quedan anónimas (`owner_id` null), sin
  publicar y con `deleted_at`, igual que un borrado normal de historia. Así esas partidas
  siguen pudiendo continuar, pero la historia ya no es de nadie ni sale en ningún sitio.
  `scripts.cleanup` la borra cuando ya no queden partidas.
- Incidentes de conducta (con su extracto) de la cuenta: se borran. Si la cuenta era dev,
  en los incidentes ajenos que resolvió se anonimiza quién lo hizo.
- Avatar y banner: los ficheros se borran después del commit. Si el commit fallara, la
  cuenta seguiría apuntando a ellos.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, inspect, select, update
from sqlalchemy.orm import Session as DbSession

from app.core import passwords
from app.core.config import settings
from app.models.auth import RefreshToken
from app.models.economy import OboloMovement, ScratchCard
from app.models.limits import ChatTurnUsage
from app.models.story import BookReview, ConductIncident, MemoryFact, Message, Story, StoryBlueprint, StoryEvent
from app.models.user import User
from app.services import custom_story_service, media_service

EXPORT_FORMAT = "psique-export-1"


class InvalidPasswordError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(obj, exclude: tuple[str, ...] = ()) -> dict:
    return {
        attr.key: getattr(obj, attr.key)
        for attr in inspect(obj).mapper.column_attrs
        if attr.key not in exclude
    }


def _all(db: DbSession, model, *where, order=None) -> list:
    return list(db.scalars(select(model).where(*where).order_by(order if order is not None else model.id)))


def export_data(db: DbSession, user: User) -> dict:
    """Todo lo que la base guarda de la cuenta. Nunca el hash de la contraseña ni tokens."""
    stories = []
    for story in _all(db, Story, Story.user_id == user.id, order=Story.created_at):
        stories.append(
            _row(story)
            | {
                "messages": [_row(m, ("story_id",)) for m in _all(db, Message, Message.story_id == story.id)],
                "facts": [_row(f, ("story_id",)) for f in _all(db, MemoryFact, MemoryFact.story_id == story.id)],
                "events": [_row(e, ("story_id",)) for e in _all(db, StoryEvent, StoryEvent.story_id == story.id)],
            }
        )

    cards = []
    for card in _all(db, ScratchCard, ScratchCard.user_id == user.id):
        # Sin revelar, el número sigue siendo secreto: exportarlo sería saber si toca antes de rascar.
        cards.append(_row(card, ("user_id",) if card.revealed_at else ("user_id", "number")))

    return {
        "format": EXPORT_FORMAT,
        "exportedAt": _now(),
        "account": _row(user, ("password_hash", "avatar_path", "banner_path"))
        | {
            "avatar_url": media_service.url_for(user.avatar_path),
            "banner_url": media_service.url_for(user.banner_path),
        },
        # Incluidas las de concepto con su perfil generado y las borradas que siguen en la base.
        "customStories": [
            _row(b, ("owner_id",))
            for b in _all(db, StoryBlueprint, StoryBlueprint.owner_id == user.id, order=StoryBlueprint.created_at)
        ],
        "stories": stories,
        "reviews": [_row(r, ("user_id",)) for r in _all(db, BookReview, BookReview.user_id == user.id)],
        "oboloMovements": [_row(m, ("user_id",)) for m in _all(db, OboloMovement, OboloMovement.user_id == user.id)],
        "scratchCards": cards,
        "conductIncidents": [
            # Con el extracto (es suyo); sin quién lo revisó, que es dato de otra persona.
            _row(i, ("user_id", "reviewed_by_id", "reviewed_by_handle"))
            for i in _all(db, ConductIncident, ConductIncident.user_id == user.id)
        ],
        "chatUsage": [
            _row(u, ("user_id",))
            for u in _all(db, ChatTurnUsage, ChatTurnUsage.user_id == user.id, order=ChatTurnUsage.day)
        ],
    }


def accept_terms(db: DbSession, user: User) -> User:
    user.terms_accepted_at = _now()
    user.terms_version = settings.TERMS_VERSION
    db.commit()
    db.refresh(user)
    return user


def delete_account(db: DbSession, user: User, password: str) -> None:
    """Borra la cuenta y lo suyo en una transacción. Pide la contraseña: un access token
    robado no basta para destruir una cuenta."""
    if not passwords.verify(password, user.password_hash):
        raise InvalidPasswordError()

    user_id = user.id
    media_paths = [user.avatar_path, user.banner_path]
    own_stories = select(Story.id).where(Story.user_id == user_id)

    db.execute(delete(ConductIncident).where(
        (ConductIncident.user_id == user_id) | ConductIncident.story_id.in_(own_stories)
    ))
    # Incidentes ajenos que revisó (si era dev): la resolución se queda, quién la tomó no.
    db.execute(
        update(ConductIncident)
        .where(ConductIncident.reviewed_by_id == user_id)
        .values(reviewed_by_id=None, reviewed_by_handle=None)
    )
    for model in (Message, MemoryFact, StoryEvent):
        db.execute(delete(model).where(model.story_id.in_(own_stories)))
    db.execute(delete(Story).where(Story.user_id == user_id))

    for model in (BookReview, ScratchCard, OboloMovement, ChatTurnUsage, RefreshToken):
        db.execute(delete(model).where(model.user_id == user_id))

    for blueprint in _all(db, StoryBlueprint, StoryBlueprint.owner_id == user_id):
        ref = custom_story_service.character_ref(blueprint.id)
        played_by_others = db.scalar(select(func.count()).select_from(Story).where(Story.character_id == ref))
        if played_by_others:
            db.execute(
                update(StoryBlueprint)
                .where(StoryBlueprint.id == blueprint.id)
                .values(owner_id=None, is_public=False, deleted_at=blueprint.deleted_at or _now())
            )
        else:
            db.execute(delete(BookReview).where(BookReview.book_id == ref))
            db.execute(delete(StoryBlueprint).where(StoryBlueprint.id == blueprint.id))

    db.execute(delete(User).where(User.id == user_id))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.expunge_all()
    for path in media_paths:
        media_service.delete_file(path)
