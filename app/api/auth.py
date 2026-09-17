from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.config import IS_PRODUCTION, settings
from app.core.database import get_db
from app.core.passwords import PasswordPolicyError
from app.core.rate_limit import RateLimiter
from app.models.user import User
from app.schemas.auth import AuthUserOut, LoginIn, SessionOut, UserRegister
from app.services import auth_service
from app.services.auth_service import AuthError

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Rutas que emiten credenciales: sin límite, probar contraseñas sale gratis.
_register_rate_limiter = RateLimiter(settings.RATE_LIMIT_MAX_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS)
_login_rate_limiter = RateLimiter(settings.RATE_LIMIT_MAX_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS)
# El refresh lo dispara el propio cliente al cargar cada pestaña: más holgado.
_refresh_rate_limiter = RateLimiter(
    settings.RATE_LIMIT_MAX_REQUESTS * 4, settings.RATE_LIMIT_WINDOW_SECONDS
)

REFRESH_COOKIE = "psique_refresh"
# Solo viaja a las rutas que lo canjean o lo revocan, no a cada petición de la API.
_COOKIE_PATH = "/api/auth"


def _set_refresh_cookie(response: Response, token: str) -> None:
    """httpOnly para que un script inyectado no pueda leerlo; SameSite=Strict para que
    otra web no pueda disparar un refresh en nombre del usuario."""
    secure = settings.COOKIE_SECURE if settings.COOKIE_SECURE is not None else IS_PRODUCTION
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.REFRESH_TOKEN_MINUTES * 60,
        path=_COOKIE_PATH,
        httponly=True,
        secure=secure,
        samesite="strict",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=_COOKIE_PATH)


def _session(response: Response, db: Session, user: User) -> SessionOut:
    access, refresh = auth_service.issue_session(db, user)
    _set_refresh_cookie(response, refresh)
    return SessionOut(access_token=access, user=AuthUserOut.from_user(user))


@router.post(
    "/register",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_register_rate_limiter)],
)
def register(payload: UserRegister, response: Response, db: Session = Depends(get_db)):
    """Devuelve ya la sesión: obligar a entrar justo después de registrarse no aporta."""
    try:
        user = auth_service.create_user(db, payload.username, payload.password, payload.display_name)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return _session(response, db, user)


@router.post("/login", response_model=SessionOut, dependencies=[Depends(_login_rate_limiter)])
def login(payload: LoginIn, response: Response, db: Session = Depends(get_db)):
    try:
        user = auth_service.authenticate(db, payload.login, payload.password)
    except AuthError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario o contraseña incorrectos."
        )
    return _session(response, db, user)


@router.post("/refresh", response_model=SessionOut, dependencies=[Depends(_refresh_rate_limiter)])
def refresh(
    response: Response,
    psique_refresh: str | None = Cookie(default=None, max_length=2048),
    db: Session = Depends(get_db),
):
    """Lo llama el cliente al cargar la página: así la sesión sobrevive a una recarga sin
    guardar ningún token en localStorage."""
    if not psique_refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No hay sesión.")
    try:
        user, access, new_refresh = auth_service.rotate_session(db, psique_refresh)
    except AuthError as exc:
        clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"set-cookie": response.headers["set-cookie"]},
        )
    _set_refresh_cookie(response, new_refresh)
    return SessionOut(access_token=access, user=AuthUserOut.from_user(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    psique_refresh: str | None = Cookie(default=None, max_length=2048),
    db: Session = Depends(get_db),
):
    if psique_refresh:
        auth_service.revoke(db, psique_refresh)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_refresh_cookie(response)
    return response
