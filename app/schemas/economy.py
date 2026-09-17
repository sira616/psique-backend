from datetime import datetime

from pydantic import BaseModel, Field


class MovementOut(BaseModel):
    id: int
    amount: int
    reason: str
    reference: str | None
    created_at: datetime | None


class WalletOut(BaseModel):
    balance: int
    movements: list[MovementOut]


class DevObolosIn(BaseModel):
    amount: int = Field(ge=1, le=1000)


class PendingCardOut(BaseModel):
    id: int
    created_at: datetime | None


class ScratchTodayOut(BaseModel):
    """Nunca lleva el número de una tarjeta: solo sale al revelarla."""

    remaining: int
    daily_limit: int
    prize: int
    winning_number: int
    pending_card: PendingCardOut | None


class ScratchCardOut(BaseModel):
    id: int
    created_at: datetime | None
    remaining: int


class RevealOut(BaseModel):
    id: int
    number: int
    won: bool
    prize: int
    balance: int
    remaining: int
