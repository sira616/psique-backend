"""Rasca y gana: cupo diario, número decidido al crear y revelado que abona una sola vez."""
from __future__ import annotations

import secrets
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.models.economy import ScratchCard
from app.models.user import User
from app.schemas.economy import PendingCardOut, RevealOut, ScratchCardOut, ScratchTodayOut
from app.services import economy_service

NO_CARDS_LEFT_MESSAGE = "Ya has rascado todas las tarjetas de hoy. Vuelve mañana."


class NoCardsLeftError(Exception):
    pass


class CardNotFoundError(Exception):
    pass


def local_today() -> date:
    return datetime.now(ZoneInfo(settings.ECONOMY_TIMEZONE)).date()


def _cards_on(db: DbSession, user_id: str, day: date) -> int:
    return db.scalar(
        select(func.count()).select_from(ScratchCard).where(ScratchCard.user_id == user_id, ScratchCard.day == day)
    )


def _remaining(db: DbSession, user_id: str, day: date) -> int:
    return max(0, settings.SCRATCH_DAILY_CARDS - _cards_on(db, user_id, day))


def _pending(db: DbSession, user_id: str, day: date) -> ScratchCard | None:
    return db.scalar(
        select(ScratchCard)
        .where(ScratchCard.user_id == user_id, ScratchCard.day == day, ScratchCard.revealed_at.is_(None))
        .order_by(ScratchCard.id)
        .limit(1)
    )


def today_out(db: DbSession, user: User) -> ScratchTodayOut:
    day = local_today()
    pending = _pending(db, user.id, day)
    return ScratchTodayOut(
        remaining=_remaining(db, user.id, day),
        daily_limit=settings.SCRATCH_DAILY_CARDS,
        prize=settings.SCRATCH_PRIZE,
        winning_number=settings.SCRATCH_WINNING_NUMBER,
        pending_card=PendingCardOut(id=pending.id, created_at=pending.created_at) if pending else None,
    )


def get_or_create(db: DbSession, user: User) -> tuple[ScratchCardOut, bool]:
    """Devuelve `(tarjeta, creada)`. Una pendiente de hoy se reutiliza: pedir otra sin
    rascar la anterior no gasta cupo.

    Si dos peticiones cogen el mismo slot, la segunda choca con el índice único, deshace y
    vuelve a mirar: entonces ve la tarjeta de la primera y la devuelve.
    """
    day = local_today()
    for _ in range(settings.SCRATCH_DAILY_CARDS + 1):
        pending = _pending(db, user.id, day)
        if pending is not None:
            return ScratchCardOut(
                id=pending.id, created_at=pending.created_at, remaining=_remaining(db, user.id, day)
            ), False
        slot = _cards_on(db, user.id, day) + 1
        if slot > settings.SCRATCH_DAILY_CARDS:
            raise NoCardsLeftError()
        card = ScratchCard(user_id=user.id, number=secrets.randbelow(10) + 1, day=day, slot=slot)
        db.add(card)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        db.refresh(card)
        return ScratchCardOut(id=card.id, created_at=card.created_at, remaining=_remaining(db, user.id, day)), True
    raise NoCardsLeftError()


def reveal(db: DbSession, user: User, card_id: int) -> RevealOut:
    card = db.get(ScratchCard, card_id)
    if card is None or card.user_id != user.id:
        raise CardNotFoundError()

    prize = settings.SCRATCH_PRIZE if card.number == settings.SCRATCH_WINNING_NUMBER else 0
    # El abono cuelga del UPDATE condicional: un segundo revelado, o uno simultáneo, no
    # encuentra la fila sin revelar y no paga otra vez.
    result = db.execute(
        update(ScratchCard)
        .where(ScratchCard.id == card.id, ScratchCard.user_id == user.id, ScratchCard.revealed_at.is_(None))
        .values(revealed_at=func.now(), prize=prize)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1 and prize > 0:
        economy_service.credit(db, user.id, prize, "rasca", f"scratch:{card.id}")
    db.commit()
    db.refresh(card)
    return RevealOut(
        id=card.id,
        number=card.number,
        won=card.prize > 0,
        prize=card.prize,
        balance=economy_service.balance(db, user.id),
        remaining=_remaining(db, user.id, local_today()),
    )
