"""Cabeceras de seguridad en cada respuesta.

El frontend guarda el refresh token en memoria, pero cualquier script inyectado podría
leerlo mientras la pestaña vive. `script-src 'self'` es lo que impide que ese script
llegue a ejecutarse, y `connect-src 'self'` que tenga a dónde mandar lo que robe.
`style-src` lleva `'unsafe-inline'` porque la interfaz usa `style={{…}}` de React.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_FUENTES_CSS = "https://fonts.googleapis.com"
_FUENTES_FICHEROS = "https://fonts.gstatic.com"

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        f"style-src 'self' 'unsafe-inline' {_FUENTES_CSS}",
        f"font-src 'self' {_FUENTES_FICHEROS}",
        "img-src 'self' data: blob:",
        # El chat por SSE va al mismo origen.
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "object-src 'none'",
    ]
)

OTRAS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}

# Swagger carga su JS de un CDN; solo existe fuera de producción.
_SIN_CSP = frozenset({"/docs", "/redoc", "/openapi.json"})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for nombre, valor in OTRAS.items():
            response.headers.setdefault(nombre, valor)
        if request.url.path not in _SIN_CSP:
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response
