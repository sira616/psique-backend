from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core import tokens
from app.core.database import get_db
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Valida el access token y devuelve la cuenta.

    401 y no 403 sin credencial: el frontend usa el 401 para decidir si refresca.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudo validar la credencial",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise credentials_exception
    try:
        payload = tokens.decode(credentials.credentials, tokens.ACCESS)
    except tokens.TokenError:
        raise credentials_exception

    user = db.get(User, payload["sub"])
    if user is None:
        raise credentials_exception
    return user


def require_dev(user: User = Depends(get_current_user)) -> User:
    """Solo cuentas con `is_dev`. 403 y no 404: la ruta no es secreta, está cerrada."""
    if not user.is_dev:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Solo para cuentas de desarrollo.")
    return user
