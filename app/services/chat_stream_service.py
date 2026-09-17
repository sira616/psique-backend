"""Un turno de la historia, emitido como eventos SSE.

Orden de un turno:
0. Solo con la partida activa y sin capítulo pendiente. La ruta ya lo comprueba antes de
   abrir el stream (409); aquí se repite por si cambió entre medias.
1. Se guarda el mensaje del usuario y el evento de turno.
2. El modelo escribe en streaming; cada frase pasa por el guardrail antes de salir.
3. Se guarda la respuesta ya filtrada.
4. Un extractor saca hechos y señales; se validan contra sus listas blancas.
5. Decisión y señales se escriben junto con el estado recalculado, en una transacción
   condicionada a que la partida siga abierta. Un cambio de fase no se aplica: queda
   pendiente hasta desbloquear el capítulo con óbolos.
6. Se emite `state` con el estado nuevo y las sugerencias de la fase, y luego `done`.

Eventos: `token {text}`, `state {...}`, `error {message}`, `done {}`.
"""
from __future__ import annotations

from collections.abc import Iterator

from app.core.config import settings
from app.core.database import SessionLocal
from app.llm import router as llm_router
from app.llm.prompts.story import build_system_prompt
from app.models.story import STORY_ACTIVE, Message, Story, StoryEvent
from app.services import story_service
from app.story import context, memory
from app.story import state_machine as sm
from app.story.guardrail import SentenceGuard, is_sincere_ai_question

LLM_UNAVAILABLE_MESSAGE = (
    "Ahora mismo la historia no puede continuar: el modelo de lenguaje no está "
    "disponible. Inténtalo de nuevo en un momento; tu mensaje se ha guardado."
)
STORY_CLOSED_MESSAGE = "Esta historia está en pausa: desbloquea el capítulo para seguir."
LLM_REFUSAL_MESSAGE = (
    "Esta parte de la escena no se puede escribir. Prueba a llevar la historia por "
    "otro lado."
)


def _event(name: str, data: dict) -> dict:
    return {"event": name, "data": data}


def stream_turn(
    story_id: str, user_message: str, choice: sm.QuickChoice | None = None
) -> Iterator[dict]:
    # Sesión propia: el generador sigue vivo después de que FastAPI cierre la de la
    # petición, y compartirla sería depender de ese orden.
    with SessionLocal() as db:
        story = db.get(Story, story_id)
        profile = story_service.story_profile(db, story) if story else None
        if story is None or profile is None:
            yield _event("error", {"message": "La historia no existe."})
            yield _event("done", {})
            return
        if story.status != STORY_ACTIVE or story.pending_phase is not None:
            yield _event("error", {"message": STORY_CLOSED_MESSAGE})
            yield _event("done", {})
            return

        turn = story.turn_count + 1
        history = context.load_messages(db, story.id)
        previous_reply = next((m.content for m in reversed(history) if m.role == "assistant"), "")

        db.add(Message(story_id=story.id, role="user", content=user_message))
        db.add(StoryEvent(story_id=story.id, kind="turno", name="mensaje", turn=turn))
        story.turn_count = turn
        db.commit()

        history = context.load_messages(db, story.id)
        sincere = is_sincere_ai_question(user_message)
        system_prompt = build_system_prompt(
            profile,
            sm.Phase(story.phase),
            story.affinity,
            memory.load_facts(db, story.id),
            context.rolling_summary(history, settings.CONTEXT_MESSAGES),
            sincere_ai_question=sincere,
        )
        window = context.build_window(history, settings.CONTEXT_MESSAGES)

        guard = SentenceGuard(allow_ai_disclosure=sincere)
        chunks = llm_router.stream(system_prompt, window)
        try:
            for chunk in chunks:
                for piece in guard.feed(chunk):
                    yield _event("token", {"text": piece})
                if guard.stopped:
                    # Fundido a negro: lo que siga del modelo no se lee ni se paga más.
                    break
            for piece in guard.flush():
                yield _event("token", {"text": piece})
        except llm_router.LLMRefusalError:
            yield _event("error", {"message": LLM_REFUSAL_MESSAGE})
            yield _event("state", story_service.state_payload(story, None))
            yield _event("done", {})
            return
        except llm_router.LLMUnavailableError:
            # El turno del usuario queda guardado; el router fusiona dos turnos de usuario
            # seguidos, así que reintentar no rompe el historial. No se evalúa el estado:
            # un fallo del modelo no puede mover la relación.
            yield _event("error", {"message": LLM_UNAVAILABLE_MESSAGE})
            yield _event("done", {})
            return
        finally:
            # Cierra la conexión con el proveedor también si se cortó por el guardrail.
            chunks.close()

        reply = guard.text
        db.add(
            Message(
                story_id=story.id,
                role="assistant",
                content=reply,
                guardrail_reason=", ".join(guard.reasons)[:80] or None,
            )
        )
        db.commit()

        extraction = memory.extract_turn(previous_reply, user_message)
        memory.save_facts(db, story.id, extraction.facts)
        db.commit()

        # La decisión va aquí y no con el mensaje: un turno que no llega a aplicarse (modelo
        # caído, o capítulo bloqueado por otro turno simultáneo) no puede puntuar después,
        # cuando se recalcule la afinidad desde los eventos.
        if choice is not None:
            db.add(StoryEvent(story_id=story.id, kind="decision", name=choice.id, turn=turn))
        for signal in extraction.signals:
            db.add(StoryEvent(story_id=story.id, kind="senal", name=signal, turn=turn))
        db.flush()

        state = sm.derive_state(story_service.load_events(db, story.id))
        # La transición no se aplica aquí: queda pendiente de pagar el capítulo.
        applied = story_service.apply_turn_state(db, story, state, sm.next_transition(state))
        if applied:
            db.commit()
        else:
            db.rollback()
        db.refresh(story)

        signals = extraction.signals if applied else []
        yield _event("state", story_service.state_payload(story, None, signals))
        yield _event("done", {})

