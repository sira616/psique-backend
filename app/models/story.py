from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
    false, func, text, true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

STORY_ACTIVE = "activa"
STORY_ARCHIVED = "archivada"


class Story(Base):
    """Una historia entre una cuenta y un personaje.

    `phase` y `affinity` son una caché de lo que calcula `app.story.state_machine` a
    partir de `story_events`: el LLM no escribe aquí nunca.
    """

    __tablename__ = "stories"
    # Una sola partida activa por (cuenta, libro): releer archiva la anterior. Índice parcial
    # y no única sin más porque las archivadas se acumulan como historial.
    __table_args__ = (
        Index(
            "uq_stories_activa",
            "user_id",
            "character_id",
            unique=True,
            sqlite_where=text("status = 'activa'"),
            postgresql_where=text("status = 'activa'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    character_id: Mapped[str] = mapped_column(String(40))
    phase: Mapped[str] = mapped_column(String(20))
    affinity: Mapped[int] = mapped_column(Integer)
    # Fase que la máquina de estados ya concedió y que espera a pagarse con óbolos. Mientras
    # no sea null, `phase` no avanza. Ver `story_service.unlock_chapter`.
    pending_phase: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    # "activa" | "archivada". Una archivada solo se lee: ni chat ni desbloqueos.
    status: Mapped[str] = mapped_column(String(12), default=STORY_ACTIVE, server_default=STORY_ACTIVE)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Resumen rodante de lo que ya no cabe en la ventana de contexto. Cubre los mensajes
    # hasta `summary_upto_message_id` (null = aún ninguno). Ver `summary_service`.
    summary: Mapped[str] = mapped_column(Text, default="")
    summary_upto_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Escena en curso y sus sugerencias ({origin, items: [{id, intent, label, message}]}),
    # generadas en el turno `suggestions_turn`. Ver `app.story.scene`.
    scene_title: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    suggestions: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    suggestions_turn: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # "user" | "assistant"
    # Para el asistente, el texto ya filtrado por el guardrail: lo que se guarda es lo que
    # se enseñó, así el historial que vuelve al modelo tampoco arrastra lo bloqueado.
    content: Mapped[str] = mapped_column(Text)
    guardrail_reason: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MemoryFact(Base):
    """Algo que el personaje recuerda de la persona. Clave de una lista blanca cerrada
    (ver `app.story.memory.FACT_KEYS`)."""

    __tablename__ = "memory_facts"
    __table_args__ = (UniqueConstraint("story_id", "key", "value", name="uq_fact"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), index=True)
    key: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StoryEvent(Base):
    """Registro del que sale el estado: turnos, señales, decisiones y transiciones.

    Es de solo añadir. Recalcular la afinidad desde aquí, en vez de ir sumando sobre la
    columna, permite cambiar los pesos y que las historias existentes los respeten.
    """

    __tablename__ = "story_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # turno | senal | decision | transicion
    name: Mapped[str] = mapped_column(String(40))
    turn: Mapped[int] = mapped_column(Integer)
    detail: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StoryBlueprint(Base):
    """Personaje e historia creados por una cuenta. Privado salvo que el dueño lo publique.

    `profile` es un `CharacterProfile` ya validado, el mismo esquema que los predefinidos.
    Las historias lo referencian con `stories.character_id = "custom:<id>"`; por eso el id
    es un uuid en hex (32) y no con guiones: cabe en los 40 caracteres de esa columna.
    """

    __tablename__ = "story_blueprints"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    mode: Mapped[str] = mapped_column(String(12))  # "definida" | "concepto"
    title: Mapped[str] = mapped_column(String(80))
    hook: Mapped[str] = mapped_column(String(140))
    premise: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    profile: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Pública = sale en Explorar y cualquier cuenta puede empezar una partida con ella.
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Cuándo se publicó por última vez: Explorar ordena por esto, no por la creación, para
    # que publicar una historia antigua la ponga arriba.
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Borrado lógico: otras cuentas pueden tener partidas empezadas con este perfil, y sin
    # él no podrían continuarlas. Ver `custom_story_service.delete_owned`.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # La primera partida de cada cuenta no cuesta óbolos; releer siempre sí.
    free_first_read: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    __table_args__ = (Index("ix_story_blueprints_explore", "is_public", "published_at"),)


class BookReview(Base):
    """Reseña de un libro (predefinido o propio). Una por cuenta y libro: volver a reseñar
    la edita. `book_id` es el mismo `character_id` de las partidas, sin FK porque los
    predefinidos no tienen fila."""

    __tablename__ = "book_reviews"
    __table_args__ = (
        UniqueConstraint("user_id", "book_id", name="uq_book_reviews_user_book"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_book_reviews_rating"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    book_id: Mapped[str] = mapped_column(String(40), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
