from __future__ import annotations

from datetime import date

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ChatTurnUsage(Base):
    """Turnos de chat que llegaron al LLM, por cuenta y día local (ECONOMY_TIMEZONE).

    En base y no en memoria: el tope existe para acotar lo que pagamos al proveedor, y un
    reinicio del proceso no puede regalar otro cupo entero. La fila se incrementa con un
    UPDATE condicionado al tope (ver `usage_service.reserve_turn`), así dos turnos
    simultáneos no pueden pasar los dos con el último hueco.
    """

    __tablename__ = "chat_turn_usage"
    __table_args__ = (CheckConstraint("turns >= 0", name="ck_chat_turn_usage_turns"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    turns: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class RateLimitWindow(Base):
    """Contador de una ventana fija del rate limiter. `expires_at` (epoch en segundos) deja
    limpiar sin saber el tamaño de ventana de cada limitador."""

    __tablename__ = "rate_limit_windows"
    __table_args__ = (Index("ix_rate_limit_windows_expires_at", "expires_at"),)

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    hits: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expires_at: Mapped[int] = mapped_column(BigInteger)
