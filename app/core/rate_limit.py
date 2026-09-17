"""Rate limiting por IP + ruta con contadores persistidos en la base (ventana fija).

Antes el contador vivía en memoria: un reinicio lo vaciaba y cada worker llevaba su propia
cuenta. Ahora cada ventana es una fila de `rate_limit_windows` y el incremento es un único
`UPDATE ... WHERE hits < max`: la base serializa las escrituras (SQLite) o bloquea la fila
(Postgres), así dos peticiones simultáneas no pueden entrar las dos con el último hueco.

Ventana fija y no deslizante: el registro de cada petición costaría una fila por hit. El
precio es que en el borde entre dos ventanas caben hasta 2x `max_requests` seguidas, que
para frenar fuerza bruta basta.
"""
# Sin `from __future__ import annotations`: FastAPI no resuelve anotaciones en texto en el
# `__call__` de una instancia y tomaría `request` por un parámetro de query.

import random
import time

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core import database
from app.models.limits import RateLimitWindow

# Probabilidad de barrer filas caducadas en una petición: lo bastante para que la tabla no
# crezca sin límite entre ejecuciones de `scripts.cleanup`, sin un DELETE en cada hit.
CLEANUP_PROBABILITY = 0.01


def insert_ignore(session: Session, model, values: dict):
    """INSERT que no falla si la clave ya existe, en SQLite y en Postgres."""
    dialect = session.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    return session.execute(insert(model).values(**values).on_conflict_do_nothing())


def delete_expired(session: Session, now: float | None = None) -> int:
    now = time.time() if now is None else now
    result = session.execute(delete(RateLimitWindow).where(RateLimitWindow.expires_at <= int(now)))
    return result.rowcount or 0


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float, time_func=time.time):
        self.max_requests = max_requests
        self.window_seconds = max(1, int(window_seconds))
        # Reloj de pared y no monotónico: las ventanas tienen que valer tras un reinicio.
        self._time_func = time_func

    def hit(self, key: str) -> bool:
        """Cuenta un intento; False si la ventana ya estaba llena."""
        now = self._time_func()
        window_start = int(now // self.window_seconds) * self.window_seconds
        with database.SessionLocal() as session:
            insert_ignore(
                session,
                RateLimitWindow,
                {"key": key, "window_start": window_start, "hits": 0, "expires_at": window_start + self.window_seconds},
            )
            result = session.execute(
                update(RateLimitWindow)
                .where(
                    RateLimitWindow.key == key,
                    RateLimitWindow.window_start == window_start,
                    RateLimitWindow.hits < self.max_requests,
                )
                .values(hits=RateLimitWindow.hits + 1)
            )
            if random.random() < CLEANUP_PROBABILITY:
                delete_expired(session, now)
            session.commit()
            return result.rowcount == 1

    def __call__(self, request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"{request.url.path}:{client_ip}"[:255]
        if not self.hit(key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Demasiados intentos. Inténtalo de nuevo en unos minutos.",
            )
