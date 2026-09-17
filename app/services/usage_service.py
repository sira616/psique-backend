"""Tope diario de turnos de chat: lo que acota el gasto en el LLM por cuenta.

Cuenta los turnos que llegan al modelo. Lo que la política o un bloqueo rechazan antes no
cuesta nada y no gasta cupo. El día es el local de España (como el rasca y gana), así que
el cupo se renueva a medianoche de Madrid y no a la del servidor.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.core.rate_limit import insert_ignore
from app.models.limits import ChatTurnUsage
from app.models.user import User

DAILY_LIMIT_MESSAGE = (
    "Has llegado al límite de turnos de hoy. La historia te espera: podrás seguir cuando se "
    "renueve el cupo."
)


def _zone() -> ZoneInfo:
    return ZoneInfo(settings.ECONOMY_TIMEZONE)


def local_today() -> date:
    return datetime.now(_zone()).date()


def resets_at(day: date | None = None) -> datetime:
    """Medianoche local del día siguiente, con su desfase (+01:00 o +02:00)."""
    day = day or local_today()
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=_zone())


def is_unlimited(user: User) -> bool:
    return bool(user.is_dev and settings.DEV_UNLIMITED_TURNS)


def turns_used(db: DbSession, user_id: str, day: date | None = None) -> int:
    day = day or local_today()
    return db.scalar(
        select(ChatTurnUsage.turns).where(ChatTurnUsage.user_id == user_id, ChatTurnUsage.day == day)
    ) or 0


def reserve_turn(db: DbSession, user: User) -> bool:
    """Gasta un turno del cupo de hoy y confirma. False si ya no quedaba.

    El UPDATE condicionado decide en la base: dos turnos simultáneos con un hueco libre
    no pueden pasar los dos, cosa que sí harían leyendo el contador y luego sumando.
    """
    day = local_today()
    insert_ignore(db, ChatTurnUsage, {"user_id": user.id, "day": day, "turns": 0})
    query = update(ChatTurnUsage).where(ChatTurnUsage.user_id == user.id, ChatTurnUsage.day == day)
    if not is_unlimited(user):
        query = query.where(ChatTurnUsage.turns < settings.CHAT_TURNS_PER_DAY)
    result = db.execute(query.values(turns=ChatTurnUsage.turns + 1).execution_options(synchronize_session=False))
    db.commit()
    return result.rowcount == 1


def refund_turn(db: DbSession, user_id: str, day: date) -> None:
    """Devuelve un turno que no llegó a generar nada (modelo caído o partida cambiada)."""
    db.execute(
        update(ChatTurnUsage)
        .where(ChatTurnUsage.user_id == user_id, ChatTurnUsage.day == day, ChatTurnUsage.turns > 0)
        .values(turns=ChatTurnUsage.turns - 1)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def usage_out(db: DbSession, user: User) -> dict:
    day = local_today()
    used = turns_used(db, user.id, day)
    unlimited = is_unlimited(user)
    limit = None if unlimited else settings.CHAT_TURNS_PER_DAY
    return {
        "turnsUsed": used,
        "turnsLimit": limit,
        "turnsRemaining": None if limit is None else max(0, limit - used),
        "unlimited": unlimited,
        "resetsAt": resets_at(day).isoformat(),
    }


def limit_reached_body() -> dict:
    return {
        "detail": DAILY_LIMIT_MESSAGE,
        "code": "daily_turn_limit",
        "resetsAt": resets_at().isoformat(),
        "turnsLimit": settings.CHAT_TURNS_PER_DAY,
    }
