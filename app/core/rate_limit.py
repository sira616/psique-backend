"""Rate limiting mínimo en memoria (ventana fija por IP + ruta).

Limitación conocida: el contador vive en memoria de un único proceso, así que
con varios workers/instancias cada uno lleva su propia cuenta (no comparten
límite). Suficiente para el MVP de un solo proceso; si se escala a varios
workers habrá que mover el contador a un backend compartido (Redis).
"""

import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request, status


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float, time_func=time.monotonic):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._time_func = time_func
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def __call__(self, request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"{request.url.path}:{client_ip}"
        now = self._time_func()
        cutoff = now - self.window_seconds

        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] < cutoff:
                hits.pop(0)

            if len(hits) >= self.max_requests:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Demasiados intentos. Inténtalo de nuevo en unos minutos.",
                )

            hits.append(now)
