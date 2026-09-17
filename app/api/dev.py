"""Herramientas de desarrollo sobre las partidas propias.

Solo cuentas `is_dev` (403 al resto) y solo partidas de quien llama: una ajena da el mismo
404 que una que no existe. Nada de esto toca óbolos. Todo queda en `story_events` con
detalle "dev", así un estado forzado se distingue después de uno jugado.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import require_dev
from app.llm.prompts.story import build_system_prompt
from app.models.story import ConductIncident, Story, StoryEvent
from app.models.user import User
from app.schemas.auth import AuthUserOut
from app.services import conduct_service, story_service
from app.story import context, memory
from app.story import state_machine as sm

router = APIRouter(prefix="/api/dev", tags=["dev"])

DEV_DETAIL = "dev"


class PhaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: Literal["conocerse", "confianza", "tension", "conflicto", "desenlace"]


class AffinityIn(BaseModel):
    """`delta` suma o resta; `value` fija la afinidad (se guarda como el delta que falta)."""

    model_config = ConfigDict(extra="forbid")

    delta: int | None = Field(default=None, ge=-100, le=100)
    value: int | None = Field(default=None, ge=sm.MIN_AFFINITY, le=sm.MAX_AFFINITY)

    @model_validator(mode="after")
    def uno_de_los_dos(self):
        if (self.delta is None) == (self.value is None):
            raise PydanticCustomError("missing", "Manda `delta` o `value`, no los dos.")
        return self


def _own_story(db: Session, user: User, story_id: str) -> Story:
    story = story_service.get_owned_story(db, user, story_id)
    if story is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Historia no encontrada.")
    return story


def _recompute(db: Session, story: Story, transition: sm.Transition | None = None) -> dict:
    db.flush()  # La sesión no hace autoflush: el evento recién añadido tiene que contar.
    state = sm.derive_state(story_service.load_events(db, story.id))
    story.affinity = state.affinity
    story.phase = state.phase.value
    db.commit()
    db.refresh(story)
    return story_service.state_payload(story, transition)


@router.post("/stories/{story_id}/phase")
def force_phase(story_id: str, payload: PhaseIn, db: Session = Depends(get_db), user: User = Depends(require_dev)):
    """Salta a cualquier fase, también hacia atrás. Descarta el capítulo pendiente."""
    story = _own_story(db, user, story_id)
    from_phase = sm.Phase(story.phase)
    db.add(
        StoryEvent(story_id=story.id, kind="transicion", name=payload.phase, turn=story.turn_count, detail=DEV_DETAIL)
    )
    story.pending_phase = None
    return _recompute(db, story, sm.Transition(from_phase, sm.Phase(payload.phase), "Forzada desde dev."))


@router.post("/stories/{story_id}/affinity")
def adjust_affinity(
    story_id: str, payload: AffinityIn, db: Session = Depends(get_db), user: User = Depends(require_dev)
):
    story = _own_story(db, user, story_id)
    current = sm.compute_affinity(story_service.load_events(db, story.id))
    delta = payload.delta if payload.delta is not None else payload.value - current
    db.add(
        StoryEvent(
            story_id=story.id, kind="ajuste", name=DEV_DETAIL, turn=story.turn_count, weight=delta, detail=DEV_DETAIL
        )
    )
    return _recompute(db, story)


@router.post("/stories/{story_id}/unlock-chapter")
def unlock_free(story_id: str, db: Session = Depends(get_db), user: User = Depends(require_dev)):
    """Como el desbloqueo normal, pero sin cobrar."""
    story = _own_story(db, user, story_id)
    if story.pending_phase is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No hay ningún capítulo por desbloquear.")
    from_phase, target = sm.Phase(story.phase), story.pending_phase
    db.add(StoryEvent(story_id=story.id, kind="transicion", name=target, turn=story.turn_count, detail=DEV_DETAIL))
    story.pending_phase = None
    return _recompute(db, story, sm.Transition(from_phase, sm.Phase(target), "Desbloqueado desde dev."))


@router.get("/stories/{story_id}/context")
def inspect_context(story_id: str, db: Session = Depends(get_db), user: User = Depends(require_dev)):
    """Lo que vería el modelo si el siguiente turno empezara ahora. En el turno real se añade
    antes el mensaje del usuario al final de `window`, y si ese mensaje es una pregunta
    sincera sobre la IA el prompt cambia esa instrucción."""
    story = _own_story(db, user, story_id)
    profile = story_service.story_profile(db, story)
    history = context.load_messages(db, story.id)
    system_prompt = None
    if profile is not None:
        system_prompt = build_system_prompt(
            profile,
            sm.Phase(story.phase),
            story.affinity,
            memory.load_facts(db, story.id),
            context.rolling_summary(
                story.summary or "", history, settings.CONTEXT_MESSAGES, story.summary_upto_message_id
            ),
            sincere_ai_question=False,
        )
    return {
        "storyId": story.id,
        "status": story.status,
        "phase": story.phase,
        "pendingPhase": story.pending_phase,
        "affinity": story.affinity,
        "turnCount": story.turn_count,
        "systemPrompt": system_prompt,
        "window": context.build_window(history, settings.CONTEXT_MESSAGES),
        "summary": story.summary or "",
        "summaryUptoMessageId": story.summary_upto_message_id,
        "sceneTitle": story.scene_title,
        "suggestions": story.suggestions,
        "suggestionsTurn": story.suggestions_turn,
    }


@router.post("/stories/{story_id}/reopen")
def reopen(story_id: str, db: Session = Depends(get_db), user: User = Depends(require_dev)):
    """Reabre una partida cerrada por la política. No borra el incidente."""
    story = _own_story(db, user, story_id)
    if not conduct_service.reopen_story(db, story):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Solo se reabre una partida cerrada y sin otra activa del mismo libro.",
        )
    return story_service.story_out(db, story)


@router.post("/me/lift-restriction", response_model=AuthUserOut)
def lift_restriction(
    clear_incidents: bool = True, db: Session = Depends(get_db), user: User = Depends(require_dev)
):
    """Levanta la restricción propia. Por defecto borra también los incidentes propios: si
    no, el siguiente cierre de prueba la volvería a activar al momento."""
    if clear_incidents:
        db.query(ConductIncident).filter(ConductIncident.user_id == user.id).delete(synchronize_session=False)
    return AuthUserOut.from_user(conduct_service.lift_restriction(db, user))
