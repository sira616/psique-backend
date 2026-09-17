"""Qué parte de la historia ve el modelo en cada turno.

Últimos `CONTEXT_MESSAGES` mensajes literales + un resumen rodante de lo anterior.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.llm.router import ChatMessage
from app.models.story import Message

# La API de Claude espera que el historial empiece por el usuario, y la historia empieza
# con el saludo del personaje. Este turno sintético lo resuelve sin tocar lo guardado.
STORY_START = "*Empieza la historia.*"
SUMMARY_MAX_CHARS = 1500


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


def rolling_summary(messages: list[Message], limit: int) -> str:
    """Resumen de lo que ya no cabe en la ventana.

    TODO: stub. Recorta cada mensaje antiguo a su primera frase y se queda con el final.
    Sustituirlo por un resumen generado por el LLM cada K turnos, guardado en
    `Story.summary` y validado (longitud, sin datos sensibles) antes de persistir.
    """
    old = messages[:-limit] if len(messages) > limit else []
    lines = []
    for m in old:
        first = m.content.strip().split("\n")[0][:140]
        who = "Usuario" if m.role == "user" else "Personaje"
        lines.append(f"{who}: {first}")
    return "\n".join(lines)[-SUMMARY_MAX_CHARS:]
