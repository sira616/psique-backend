from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OboloMovement(Base):
    """Cada óbolo que entra o sale. De solo añadir: `users.obolos` debe ser siempre la suma
    de los movimientos de la cuenta, y así se puede auditar o reconstruir."""

    __tablename__ = "obolo_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(20))  # bienvenida | rasca | capitulo | lectura | ajuste_dev
    # Qué originó el movimiento, p. ej. "scratch:12" o "story:<id>:confianza".
    reference: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ScratchCard(Base):
    """Tarjeta de rasca y gana. El número se decide al crearla y no sale del servidor hasta
    revelarla: el cliente solo rasca la pintura.

    `slot` (1..SCRATCH_DAILY_CARDS) con el índice único es lo que hace cumplir el cupo
    diario con peticiones simultáneas: dos creaciones que cuenten a la vez intentan el
    mismo slot y una falla, en vez de colarse las dos.
    """

    __tablename__ = "scratch_cards"
    __table_args__ = (
        UniqueConstraint("user_id", "day", "slot", name="uq_scratch_cards_user_day_slot"),
        CheckConstraint("number BETWEEN 1 AND 10", name="ck_scratch_cards_number"),
        CheckConstraint("slot >= 1", name="ck_scratch_cards_slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    # Día local (ECONOMY_TIMEZONE), no UTC: es lo que ve la persona como "hoy".
    day: Mapped[date] = mapped_column(Date)
    slot: Mapped[int] = mapped_column(Integer)
    revealed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    prize: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
