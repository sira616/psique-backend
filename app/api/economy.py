from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.security import get_current_user, require_dev
from app.models.user import User
from app.schemas.economy import DevObolosIn, RevealOut, ScratchCardOut, ScratchTodayOut, WalletOut
from app.services import economy_service, scratch_service

router = APIRouter(prefix="/api", tags=["economy"])

# El cupo diario ya limita lo que se gana; esto solo frena a quien machaca el endpoint.
_scratch_rate_limiter = RateLimiter(settings.RATE_LIMIT_MAX_REQUESTS * 4, settings.RATE_LIMIT_WINDOW_SECONDS)


@router.get("/me/wallet", response_model=WalletOut)
def get_wallet(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return economy_service.wallet_out(db, user.id)


@router.post("/dev/obolos", response_model=WalletOut)
def grant_dev_obolos(payload: DevObolosIn, db: Session = Depends(get_db), user: User = Depends(require_dev)):
    economy_service.credit(db, user.id, payload.amount, "ajuste_dev")
    db.commit()
    return economy_service.wallet_out(db, user.id)


@router.get("/scratch-cards/today", response_model=ScratchTodayOut)
def scratch_today(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return scratch_service.today_out(db, user)


@router.post(
    "/scratch-cards",
    response_model=ScratchCardOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_scratch_rate_limiter)],
)
def create_scratch_card(response: Response, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        card, created = scratch_service.get_or_create(db, user)
    except scratch_service.NoCardsLeftError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=scratch_service.NO_CARDS_LEFT_MESSAGE)
    if not created:
        response.status_code = status.HTTP_200_OK
    return card


@router.post(
    "/scratch-cards/{card_id}/reveal", response_model=RevealOut, dependencies=[Depends(_scratch_rate_limiter)]
)
def reveal_scratch_card(card_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        return scratch_service.reveal(db, user, card_id)
    except scratch_service.CardNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tarjeta no encontrada.")
