from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.models.story import STORY_ACTIVE, STORY_ARCHIVED, MemoryFact, Message, Story, StoryEvent
from app.models.user import User
from app.services import custom_story_service, economy_service
from app.schemas.story import (
    CharacterOut,
    FactOut,
    MessageOut,
    QuickChoiceOut,
    StoryOut,
    StoryStateOut,
    StorySummaryOut,
)
from app.story import state_machine as sm
from app.story.character_profile import CharacterProfile, get_character, load_characters


def character_out(profile: CharacterProfile) -> CharacterOut:
    return CharacterOut(
        id=profile.id,
        origin="psique",
        title=profile.nombre,
        hook=profile.tagline,
        name=profile.nombre,
        age=profile.edad,
        tagline=profile.tagline,
        traits=list(profile.personalidad),
        scenario=profile.escenario_inicial,
    )


def list_characters(db: DbSession, user: User) -> list[CharacterOut]:
    predefined = [character_out(p) for p in load_characters().values()]
    own = [custom_story_service.character_out(b) for b in custom_story_service.list_owned(db, user)]
    return predefined + own


def resolve_profile(db: DbSession, user_id: str, character_id: str) -> CharacterProfile | None:
    """Perfil para empezar una partida: predefinido, propio o público de otra cuenta."""
    if character_id.startswith(custom_story_service.CUSTOM_PREFIX):
        return custom_story_service.resolve_profile(db, user_id, character_id)
    return get_character(character_id)


def story_profile(db: DbSession, story: Story) -> CharacterProfile | None:
    """Perfil para continuar una partida existente, aunque su autor la haya despublicado
    o borrado."""
    if story.character_id.startswith(custom_story_service.CUSTOM_PREFIX):
        return custom_story_service.resolve_story_profile(db, story.character_id)
    return get_character(story.character_id)


# --- Partidas por libro -------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def free_first_read(db: DbSession, character_id: str) -> bool:
    if character_id.startswith(custom_story_service.CUSTOM_PREFIX):
        blueprint = custom_story_service.blueprint_for(db, character_id)
        return bool(blueprint and blueprint.free_first_read)
    profile = get_character(character_id)
    return bool(profile and profile.free_first_read)


def active_story(db: DbSession, user_id: str, character_id: str) -> Story | None:
    return db.scalar(
        select(Story).where(
            Story.user_id == user_id, Story.character_id == character_id, Story.status == STORY_ACTIVE
        )
    )


def has_read(db: DbSession, user_id: str, character_id: str) -> bool:
    """Si la cuenta tuvo alguna vez una partida de ese libro, activa o archivada."""
    return bool(
        db.scalar(select(exists().where(Story.user_id == user_id, Story.character_id == character_id)))
    )


def start_cost(db: DbSession, user_id: str, character_id: str) -> int:
    if free_first_read(db, character_id) and not has_read(db, user_id, character_id):
        return 0
    return settings.READ_COST


def _add_story(db: DbSession, user: User, character_id: str, profile: CharacterProfile, cost: int) -> Story:
    """Añade la partida y su cobro a la transacción en curso, sin confirmar."""
    story = Story(
        id=str(uuid.uuid4()),
        user_id=user.id,
        character_id=character_id,
        phase=sm.Phase.CONOCERSE.value,
        affinity=sm.compute_affinity([]),
        turn_count=0,
        summary="",
        status=STORY_ACTIVE,
    )
    # Primero el cobro: sin saldo no llega a escribirse nada.
    if cost > 0:
        economy_service.spend(db, user.id, cost, "lectura", story.id)
    db.add(story)
    db.flush()
    db.add(Message(story_id=story.id, role="assistant", content=profile.saludo))
    return story


def start_story(db: DbSession, user: User, character_id: str, profile: CharacterProfile) -> tuple[Story, bool]:
    """Devuelve la partida activa de ese libro o crea una; el bool dice si se creó.

    Idempotente: repetir la petición (doble clic, reintento) devuelve la misma partida sin
    cobrar otra vez. Dos peticiones simultáneas chocan en `uq_stories_activa`; la que pierde
    deshace también su cobro y devuelve la que ganó.
    """
    existing = active_story(db, user.id, character_id)
    if existing is not None:
        return existing, False
    cost = start_cost(db, user.id, character_id)
    try:
        story = _add_story(db, user, character_id, profile, cost)
        db.commit()
    except economy_service.InsufficientObolosError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        existing = active_story(db, user.id, character_id)
        if existing is None:
            raise
        return existing, False
    db.refresh(story)
    return story, True


class NotStartedError(Exception):
    pass


def reread(db: DbSession, user: User, character_id: str, profile: CharacterProfile) -> Story:
    """Archiva la partida activa y empieza otra cobrando `READ_COST`, en una transacción:
    sin saldo no se archiva nada.

    Si otra relectura simultánea gana el índice único, esta se deshace entera (cobro
    incluido) y sale `IntegrityError`: no se archiva lo que la otra acaba de empezar.
    """
    if not has_read(db, user.id, character_id):
        raise NotStartedError()
    try:
        db.execute(
            update(Story)
            .where(Story.user_id == user.id, Story.character_id == character_id, Story.status == STORY_ACTIVE)
            .values(status=STORY_ARCHIVED, archived_at=_now())
            .execution_options(synchronize_session=False)
        )
        story = _add_story(db, user, character_id, profile, settings.READ_COST)
        db.commit()
    except (economy_service.InsufficientObolosError, IntegrityError):
        db.rollback()
        raise
    db.refresh(story)
    return story


def get_owned_story(db: DbSession, user: User, story_id: str) -> Story | None:
    story = db.get(Story, story_id)
    # La historia de otra cuenta es, a efectos de quien pregunta, una que no existe.
    if story is None or story.user_id != user.id:
        return None
    return story


def load_events(db: DbSession, story_id: str) -> list[sm.Event]:
    rows = db.scalars(
        select(StoryEvent).where(StoryEvent.story_id == story_id).order_by(StoryEvent.id)
    )
    return [sm.Event(kind=r.kind, name=r.name, turn=r.turn) for r in rows]


def state_out(story: Story) -> StoryStateOut:
    phase = sm.Phase(story.phase)
    return StoryStateOut(
        phase=phase.value,
        phaseLabel=sm.PHASE_LABELS[phase],
        phaseIndex=sm.PHASE_ORDER.index(phase),
        phaseCount=len(sm.PHASE_ORDER),
        affinity=story.affinity,
        turnCount=story.turn_count,
        quickChoices=[QuickChoiceOut(id=c.id, label=c.label) for c in sm.choices_for(phase)],
        chapter_locked=story.pending_phase is not None,
        next_phase=story.pending_phase,
        chapter_cost=settings.CHAPTER_COST,
    )


def state_payload(story: Story, transition: sm.Transition | None, signals: list[str] | None = None) -> dict:
    """Lo que viaja en el evento SSE `state` y en la respuesta de desbloquear capítulo."""
    payload = state_out(story).model_dump()
    payload["transition"] = (
        {
            "from": transition.from_phase.value,
            "to": transition.to_phase.value,
            "reason": transition.reason,
        }
        if transition
        else None
    )
    payload["signals"] = signals or []
    return payload


def apply_turn_state(
    db: DbSession, story: Story, state: sm.StoryState, transition: sm.Transition | None
) -> bool:
    """Lleva a la historia lo que calculó la máquina de estados, con los capítulos de pago.

    Una transición no se aplica: queda en `pending_phase` y `phase` sigue igual. Con un
    capítulo pendiente la historia se congela (el chat responde 409), así que la afinidad
    guardada es la del turno que lo ganó. Tampoco se registra el evento `transicion` hasta
    pagar: `derive_state` sale de esos eventos y avanzaría la fase.

    Es un UPDATE condicionado a que la partida siga abierta: un turno que empezó antes de
    que otro bloqueara el capítulo (o de que se archivara) no escribe encima. Devuelve si se
    aplicó; si no, quien llama deshace lo que ese turno aún no ha confirmado.
    """
    values = {"affinity": state.affinity, "phase": state.phase.value}
    if transition is not None:
        values["pending_phase"] = transition.to_phase.value
    result = db.execute(
        update(Story)
        .where(Story.id == story.id, Story.pending_phase.is_(None), Story.status == STORY_ACTIVE)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


class ChapterNotLockedError(Exception):
    pass


class StoryArchivedError(Exception):
    pass


def unlock_chapter(db: DbSession, user: User, story: Story) -> tuple[sm.Transition, int]:
    """Cobra `CHAPTER_COST` y avanza a la fase pendiente, todo en una transacción.

    El avance es un UPDATE condicionado a que la fase pendiente siga ahí: dos desbloqueos
    simultáneos no cobran dos veces, el segundo no encuentra nada que desbloquear.
    """
    if story.status != STORY_ACTIVE:
        raise StoryArchivedError()
    target = story.pending_phase
    if target is None:
        raise ChapterNotLockedError()
    from_phase = sm.Phase(story.phase)
    claimed = db.execute(
        update(Story)
        .where(Story.id == story.id, Story.pending_phase == target, Story.status == STORY_ACTIVE)
        .values(phase=target, pending_phase=None)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise ChapterNotLockedError()
    try:
        economy_service.spend(db, user.id, settings.CHAPTER_COST, "capitulo", f"story:{story.id}:{target}")
    except economy_service.InsufficientObolosError:
        db.rollback()
        raise

    # La razón de la regla, si sigue valiendo, es más útil en el registro que un "pagado".
    rule = sm.next_transition(sm.derive_state(load_events(db, story.id)))
    reason = rule.reason if rule and rule.to_phase.value == target else "Capítulo desbloqueado."
    transition = sm.Transition(from_phase, sm.Phase(target), reason)
    db.add(
        StoryEvent(story_id=story.id, kind="transicion", name=target, turn=story.turn_count, detail=reason)
    )
    db.commit()
    db.refresh(story)
    return transition, economy_service.balance(db, user.id)


def _character_name(db: DbSession, story: Story) -> str:
    profile = story_profile(db, story)
    return profile.nombre if profile else story.character_id


def story_out(db: DbSession, story: Story) -> StoryOut:
    messages = db.scalars(select(Message).where(Message.story_id == story.id).order_by(Message.id))
    facts = db.scalars(select(MemoryFact).where(MemoryFact.story_id == story.id).order_by(MemoryFact.id))
    return StoryOut(
        id=story.id,
        characterId=story.character_id,
        characterName=_character_name(db, story),
        state=state_out(story),
        messages=[
            MessageOut(id=m.id, role=m.role, content=m.content, createdAt=m.created_at) for m in messages
        ],
        facts=[FactOut(key=f.key, value=f.value) for f in facts],
        createdAt=story.created_at,
        status=story.status,
        archivedAt=story.archived_at,
    )


def list_stories(db: DbSession, user: User) -> list[StorySummaryOut]:
    """Solo las activas: las archivadas son historial y se ven en la página del libro."""
    stories = db.scalars(
        select(Story)
        .where(Story.user_id == user.id, Story.status == STORY_ACTIVE)
        .order_by(Story.updated_at.desc())
    )
    return [
        StorySummaryOut(
            id=s.id,
            characterId=s.character_id,
            characterName=_character_name(db, s),
            state=state_out(s),
            updatedAt=s.updated_at,
            status=s.status,
            archivedAt=s.archived_at,
        )
        for s in stories
    ]
