"""Perfil público: handle, bio, enlace y estanterías.

El handle es la identidad pública y `username` el login. Están separados para que
cambiar cómo te encuentran no cambie cómo entras, y para que el perfil no enseñe el
nombre con el que se inicia sesión.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.models.story import STORY_ACTIVE, Story, StoryBlueprint
from app.models.user import User
from app.schemas.profile import (
    MyProfileOut,
    ProfileOut,
    ProfilePatchIn,
    ProgressOut,
    PublishedShelfOut,
    ReadingItemOut,
    ReadingShelfOut,
    ShelvesOut,
)
from app.services import custom_story_service, media_service
from app.story import state_machine as sm
from app.story.character_profile import get_character
from app.story.moderation import check_user_text

# Cada estantería enseña las más recientes; un perfil no es un listado paginado.
SHELF_LIMIT = 50
HANDLE_TAKEN = "Ese handle ya está cogido."


class ProfileError(Exception):
    """Errores por campo, con la misma forma que los de validación de FastAPI."""

    def __init__(self, status_code: int, errors: list[dict]):
        super().__init__(errors[0]["msg"])
        self.status_code = status_code
        self.errors = errors


def field_error(field: str, msg: str, type_: str) -> dict:
    return {"loc": ["body", field], "msg": msg, "type": type_}


def handle_from_username(username: str) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", username.lower())[:30]
    return base if len(base) >= 3 else (base + "___")[:3]


def unique_handle(db: DbSession, username: str) -> str:
    base = handle_from_username(username)
    handle, n = base, 2
    # Otra cuenta puede haber elegido a mano el handle que le tocaría a esta.
    while db.scalar(select(User.id).where(User.handle == handle)):
        suffix = f"_{n}"
        handle = base[: 30 - len(suffix)] + suffix
        n += 1
    return handle


def get_by_handle(db: DbSession, handle: str) -> User | None:
    return db.scalar(select(User).where(User.handle == handle.strip().lower()))


def my_profile_out(user: User) -> MyProfileOut:
    return MyProfileOut(
        handle=user.handle,
        displayName=user.display_name,
        bio=user.bio,
        link=user.link,
        avatarUrl=media_service.url_for(user.avatar_path),
        bannerUrl=media_service.url_for(user.banner_path),
        showPublished=user.show_published,
        showReading=user.show_reading,
    )


def update(db: DbSession, user: User, patch: ProfilePatchIn) -> None:
    changes = patch.model_dump(exclude_unset=True)

    errors = []
    for field in ("displayName", "bio", "handle"):
        value = changes.get(field)
        # En el handle el guion bajo hace de espacio: "sexo_gratis" también es una palabra.
        rejection = check_user_text(value.replace("_", " ") if field == "handle" and value else value)
        if rejection:
            errors.append(field_error(field, rejection, "content_policy"))
    if errors:
        raise ProfileError(422, errors)

    handle = changes.get("handle")
    if handle is not None and handle != user.handle:
        if db.scalar(select(User.id).where(User.handle == handle, User.id != user.id)):
            raise ProfileError(409, [field_error("handle", HANDLE_TAKEN, "handle_taken")])
        user.handle = handle

    columns = {"displayName": "display_name", "bio": "bio", "link": "link",
               "showPublished": "show_published", "showReading": "show_reading"}
    for field, column in columns.items():
        if field in changes:
            setattr(user, column, changes[field])
    try:
        db.commit()
    except IntegrityError:
        # Dos cuentas pidiendo el mismo handle a la vez: la comprobación de arriba no basta.
        db.rollback()
        raise ProfileError(409, [field_error("handle", HANDLE_TAKEN, "handle_taken")])
    db.refresh(user)


def _published(db: DbSession, owner: User, viewer: User):
    rows, _ = custom_story_service.list_public(
        db,
        limit=SHELF_LIMIT,
        offset=0,
        owner_id=owner.id,
        include_adult=viewer.id == owner.id or viewer.adult_confirmed_at is not None,
    )
    return [custom_story_service.card_out(b, author, viewer.id) for b, author in rows]


def _reading(db: DbSession, owner: User, is_owner: bool) -> list[ReadingItemOut]:
    stories = list(
        db.scalars(
            select(Story)
            .where(Story.user_id == owner.id, Story.status == STORY_ACTIVE)
            .order_by(Story.updated_at.desc(), Story.id)
        )
    )
    blueprint_ids = [
        c[len(custom_story_service.CUSTOM_PREFIX):]
        for c in {s.character_id for s in stories}
        if c.startswith(custom_story_service.CUSTOM_PREFIX)
    ]
    blueprints = {
        b.id: b
        for b in db.scalars(select(StoryBlueprint).where(StoryBlueprint.id.in_(blueprint_ids)))
    } if blueprint_ids else {}

    items = []
    for story in stories:
        item = _reading_item(story, blueprints, is_owner)
        if item is not None:
            items.append(item)
        if len(items) >= SHELF_LIMIT:
            break
    return items


def _reading_item(story: Story, blueprints: dict, is_owner: bool) -> ReadingItemOut | None:
    phase = sm.Phase(story.phase)
    progress = ProgressOut(
        phase=phase.value,
        phaseLabel=sm.PHASE_LABELS[phase],
        phaseIndex=sm.PHASE_ORDER.index(phase),
        phaseCount=len(sm.PHASE_ORDER),
    )
    common = {"characterId": story.character_id, "progress": progress, "updatedAt": story.updated_at}

    if not story.character_id.startswith(custom_story_service.CUSTOM_PREFIX):
        profile = get_character(story.character_id)
        if profile is None:
            return None
        return ReadingItemOut(**common, origin="psique", mode=None, title=profile.nombre, characterName=profile.nombre)

    blueprint = blueprints.get(story.character_id[len(custom_story_service.CUSTOM_PREFIX):])
    if blueprint is None:
        return None
    # Para otras cuentas, una partida con una historia privada o borrada no existe: su
    # título es del autor, que no la ha publicado.
    if not is_owner and (not blueprint.is_public or blueprint.deleted_at is not None):
        return None
    name = None
    if blueprint.mode == "definida":
        name = custom_story_service.profile_of(blueprint).nombre
    return ReadingItemOut(
        **common,
        origin="propia",
        mode=blueprint.mode,
        title=blueprint.title,
        characterName=name,
        coverUrl=media_service.url_for(blueprint.cover_path),
    )


def profile_out(db: DbSession, owner: User, viewer: User) -> ProfileOut:
    is_owner = owner.id == viewer.id
    show_published = is_owner or owner.show_published
    show_reading = is_owner or owner.show_reading
    return ProfileOut(
        handle=owner.handle,
        displayName=owner.display_name,
        bio=owner.bio,
        link=owner.link,
        avatarUrl=media_service.url_for(owner.avatar_path),
        bannerUrl=media_service.url_for(owner.banner_path),
        joinedAt=owner.created_at,
        isOwner=is_owner,
        shelves=ShelvesOut(
            published=PublishedShelfOut(
                items=_published(db, owner, viewer) if show_published else None,
                visibleToOthers=owner.show_published,
            ),
            reading=ReadingShelfOut(
                items=_reading(db, owner, is_owner) if show_reading else None,
                visibleToOthers=owner.show_reading,
            ),
        ),
    )
