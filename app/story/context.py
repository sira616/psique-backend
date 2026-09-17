"""Qué parte de la historia ve el modelo en cada turno.

Últimos `CONTEXT_MESSAGES` mensajes literales + el resumen rodante de lo anterior
(`Story.summary`, que cubre hasta `Story.summary_upto_message_id`). Lo que ya salió de la
ventana pero aún no está resumido entra recortado, para que no se pierda nada mientras
llega el siguiente plegado (ver `app.services.summary_service`).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.llm.router import ChatMessage
from app.models.story import Message

# La API de Claude espera que el historial empiece por el usuario, y la historia empieza
# con el saludo del personaje. Este turno sintético lo resuelve sin tocar lo guardado.
STORY_START = "*Empieza la historia.*"
# 1500 se quedaba corto para una historia de muchos capítulos; 2000 son unos 500 tokens.
SUMMARY_MAX_CHARS = 2000


def load_messages(db: DbSession, story_id: str) -> list[Message]:
    return list(
        db.scalars(select(Message).where(Message.story_id == story_id).order_by(Message.id))
    )


def build_window(messages: list[Message], limit: int) -> list[ChatMessage]:
    window: list[ChatMessage] = [
        {"role": m.role, "content": m.content}  # type: ignore[typeddict-item]
        for m in messages[-limit:]
        if m.role in ("user", "assistant")
    ]
    if window and window[0]["role"] == "assistant":
        window.insert(0, {"role": "user", "content": STORY_START})
    return window


def unsummarized(messages: list[Message], limit: int, upto_id: int | None) -> list[Message]:
    """Mensajes fuera de la ventana que el resumen guardado aún no cubre, del más antiguo
    al más reciente."""
    old = messages[:-limit] if len(messages) > limit else []
    return [m for m in old if upto_id is None or m.id > upto_id]


def clip_messages(messages: list[Message]) -> str:
    """Recorte barato: primera línea de cada mensaje y el final. Solo cubre el hueco que el
    resumen aún no ha plegado, y es también el resumen del modo demo."""
    lines = []
    for m in messages:
        first = m.content.strip().split("\n")[0][:140]
        who = "Usuario" if m.role == "user" else "Personaje"
        lines.append(f"{who}: {first}")
    return "\n".join(lines)[-SUMMARY_MAX_CHARS:]


def rolling_summary(summary: str, messages: list[Message], limit: int, upto_id: int | None) -> str:
    gap = clip_messages(unsummarized(messages, limit, upto_id))
    return "\n\n".join(part for part in (summary.strip(), gap) if part)
