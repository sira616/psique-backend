"""Resumen rodante: pliega en `Story.summary` lo que va saliendo de la ventana de contexto.

Cuándo: al final de un turno, ya emitido `done` y después de la extracción (Ollama Cloud
gratuito admite una sola petición a la vez). Solo cuando los mensajes fuera de la ventana
sin resumir llegan a `SUMMARY_BATCH_MESSAGES`: una llamada al LLM recibe el resumen
anterior y esos mensajes, y devuelve el resumen nuevo.

Si la llamada o la validación fallan, el resumen y su marcador se quedan como estaban y
se reintenta en un turno posterior, cuando vuelva a tocar. Mientras tanto
`context.rolling_summary` cubre el hueco con el recorte barato, así que no se pierde nada.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import update
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.llm import router as llm_router
from app.llm.prompts.summary import SUMMARY_SYSTEM_PROMPT, build_summary_input
from app.models.story import Message, Story
from app.story import context
from app.story.content_policy import is_explicit
from app.story.guardrail import contains_sensitive, redact_sensitive

logger = logging.getLogger(__name__)

# Tope de mensajes por plegado: si varios lotes fallaron seguidos, se pone al día en una
# llamada sin mandar un historial entero.
MAX_FOLD_MESSAGES = 30

# Algunos modelos anteponen "Resumen:" o "**Resumen actualizado:**" aunque se les pida
# solo el texto.
_LABEL = re.compile(r"^\**\s*resumen[^:\n]{0,25}:\**\s*", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?…](?:[*»\"”)]+)?(?=\s|$)")


def validate_summary(raw: str | None) -> str | None:
    """Texto listo para guardar, o None si no se puede guardar."""
    text = _LABEL.sub("", (raw or "").strip().strip("`")).strip()
    if not text:
        return None
    if len(text) > context.SUMMARY_MAX_CHARS:
        ends = [m.end() for m in _SENTENCE_END.finditer(text, 0, context.SUMMARY_MAX_CHARS)]
        if not ends:
            return None
        text = text[: ends[-1]]
    # Un resumen explícito o con datos personales se leería en cada turno siguiente: se
    # descarta entero en vez de intentar arreglarlo.
    if is_explicit(text) or contains_sensitive(text):
        return None
    return text


def _fragment(messages: list[Message], character_name: str) -> list[tuple[str, str]]:
    return [
        ("La persona" if m.role == "user" else character_name, redact_sensitive(m.content.strip()))
        for m in messages
        if m.role in ("user", "assistant") and m.content.strip()
    ]


def _demo_summary(previous: str, batch: list[Message]) -> str:
    # Sin modelo real: determinista, y se queda con lo más reciente por líneas enteras.
    text = "\n".join(part for part in (previous.strip(), context.clip_messages(batch)) if part)
    lines = text.split("\n")
    while len("\n".join(lines)) > context.SUMMARY_MAX_CHARS and len(lines) > 1:
        lines.pop(0)
    return "\n".join(lines)[-context.SUMMARY_MAX_CHARS :]


def fold_if_due(db: DbSession, story_id: str, character_name: str) -> bool:
    """Pliega el siguiente lote si toca. Devuelve si el resumen avanzó."""
    story = db.get(Story, story_id)
    if story is None:
        return False
    messages = context.load_messages(db, story.id)
    pending = context.unsummarized(messages, settings.CONTEXT_MESSAGES, story.summary_upto_message_id)
    if len(pending) < settings.SUMMARY_BATCH_MESSAGES:
        return False
    batch = pending[:MAX_FOLD_MESSAGES]
    previous, previous_upto = story.summary or "", story.summary_upto_message_id

    if llm_router.demo_mode():
        raw = _demo_summary(previous, batch)
    else:
        try:
            raw = llm_router.generate(
                SUMMARY_SYSTEM_PROMPT,
                [{"role": "user", "content": build_summary_input(previous, _fragment(batch, character_name), character_name)}],
                max_tokens=1024,
            )
        except llm_router.LLMUnavailableError as exc:
            logger.warning("Resumen de %s no generado: %s", story.id, exc)
            return False
    summary = validate_summary(raw)
    if summary is None:
        logger.warning("Resumen de %s descartado en la validación", story.id)
        return False

    # Condicionado al marcador leído: si otro turno plegó entre medias, no se pisa su
    # resumen con uno hecho sobre el anterior.
    same_marker = (
        Story.summary_upto_message_id.is_(None)
        if previous_upto is None
        else Story.summary_upto_message_id == previous_upto
    )
    result = db.execute(
        update(Story)
        .where(Story.id == story.id, same_marker)
        .values(summary=summary, summary_upto_message_id=batch[-1].id)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def fold_safely(db: DbSession, story_id: str, character_name: str) -> bool:
    """Como `fold_if_due`, pero nunca lanza: corre con el turno ya entregado y un fallo
    aquí no puede convertirse en un error del stream."""
    try:
        return fold_if_due(db, story_id, character_name)
    except Exception:
        logger.exception("Fallo inesperado al plegar el resumen de %s", story_id)
        db.rollback()
        return False
