from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.profile import ExplorePageOut
from app.services import conduct_service
from app.services import custom_story_service as svc

router = APIRouter(prefix="/api/explore", tags=["explore"])


@router.get("", response_model=ExplorePageOut)
def explore(
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10_000),
    mode: Literal["definida", "concepto"] | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Historias públicas de todas las cuentas, las publicadas más recientemente primero.

    Qué campos salen de cada modo está decidido en `custom_story_service.card_out`: en
    concepto nunca la premisa ni nada del perfil inventado por el LLM.

    Paginación por limit/offset: el volumen es pequeño y permite saltar a una página. Si
    alguien publica mientras se pagina, una tarjeta puede repetirse en la página siguiente;
    el cliente deduplica por `id`.
    """
    rows, has_more = svc.list_public(
        db, limit=limit, offset=offset, mode=mode, include_adult=conduct_service.adult_confirmed(user)
    )
    return ExplorePageOut(
        items=[svc.card_out(b, author, user.id) for b, author in rows],
        limit=limit,
        offset=offset,
        nextOffset=offset + limit if has_more else None,
    )
