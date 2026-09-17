"""Óbolos: la única puerta para mover saldo.

Cada función cambia `users.obolos` y añade su `OboloMovement` en la sesión que recibe, sin
hacer commit: quien llama decide la transacción, y así el cobro de un capítulo y su avance
se confirman o se deshacen juntos.

El gasto no lee el saldo para luego restarlo: con dos peticiones a la vez las dos leerían
saldo suficiente. Un único `UPDATE ... WHERE obolos >= :coste` deja que la base decida y
solo una de las dos toca la fila.
"""
from __future__ import annotations

from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from app.models.economy import OboloMovement
from app.models.user import User
from app.schemas.economy import MovementOut, WalletOut

Reason = Literal["bienvenida", "rasca", "capitulo", "lectura", "ajuste_dev"]

WALLET_MOVEMENTS = 20


class InsufficientObolosError(Exception):
    pass


def credit(db: DbSession, user_id: str, amount: int, reason: Reason, reference: str | None = None) -> None:
    if amount <= 0:
        raise ValueError("Un abono tiene que ser positivo")
    db.execute(
        update(User)
        .where(User.id == user_id)
        .values(obolos=User.obolos + amount)
        .execution_options(synchronize_session=False)
    )
    db.add(OboloMovement(user_id=user_id, amount=amount, reason=reason, reference=reference))
    db.flush()


def spend(db: DbSession, user_id: str, amount: int, reason: Reason, reference: str | None = None) -> None:
    """Resta `amount` o lanza `InsufficientObolosError` sin haber tocado el saldo."""
    if amount <= 0:
        raise ValueError("Un gasto tiene que ser positivo")
    result = db.execute(
        update(User)
        .where(User.id == user_id, User.obolos >= amount)
        .values(obolos=User.obolos - amount)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise InsufficientObolosError()
    db.add(OboloMovement(user_id=user_id, amount=-amount, reason=reason, reference=reference))
    db.flush()


def balance(db: DbSession, user_id: str) -> int:
    # De la base y no de `user.obolos`: los UPDATE de arriba no refrescan el objeto cargado.
    return db.scalar(select(User.obolos).where(User.id == user_id)) or 0


def wallet_out(db: DbSession, user_id: str) -> WalletOut:
    movements = db.scalars(
        select(OboloMovement)
        .where(OboloMovement.user_id == user_id)
        .order_by(OboloMovement.id.desc())
        .limit(WALLET_MOVEMENTS)
    )
    return WalletOut(
        balance=balance(db, user_id),
        movements=[
            MovementOut(id=m.id, amount=m.amount, reason=m.reason, reference=m.reference, created_at=m.created_at)
            for m in movements
        ],
    )
