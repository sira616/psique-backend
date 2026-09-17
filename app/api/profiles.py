from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.profile import ProfileOut
from app.services import profile_service

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("/{handle}", response_model=ProfileOut)
def get_profile(
    handle: str = Path(max_length=30),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    owner = profile_service.get_by_handle(db, handle)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Perfil no encontrado.")
    return profile_service.profile_out(db, owner, user)
