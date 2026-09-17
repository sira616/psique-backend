from contextlib import asynccontextmanager

import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import app.models  # noqa: F401 — registra los modelos en Base.metadata
from app.api import auth, books, custom_stories, economy, explore, me, profiles, stories
from app.core.config import IS_PRODUCTION, settings
from app.core.headers import SecurityHeadersMiddleware
from app.core.migrations import upgrade_to_head
from app.story.character_profile import load_characters


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Migrar y no `create_all`: create_all no cambia tablas que ya existen.
    upgrade_to_head()
    # Cargar los perfiles al arrancar: un JSON inválido tumba el arranque, no una escena.
    load_characters()
    yield


_docs_enabled = not IS_PRODUCTION

app = FastAPI(
    title="Psique — Backend",
    description="Historias románticas interactivas para todos los públicos.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)
# Después del CORS, así que envuelve por fuera: cabeceras también en las respuestas de CORS.
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth.router)
app.include_router(me.router)
app.include_router(stories.router)
app.include_router(custom_stories.router)
app.include_router(explore.router)
app.include_router(profiles.router)
app.include_router(economy.router)
app.include_router(books.router)

# El registro de tipos de Windows no conoce .webp: sin esto se serviría como
# application/octet-stream y, con nosniff, el navegador no lo pintaría.
mimetypes.add_type("image/webp", ".webp")
# StaticFiles exige que el directorio exista al montarlo.
Path(settings.MEDIA_DIR).mkdir(parents=True, exist_ok=True)
# Solo contiene WebP re-codificados por `media_service`; nunca un fichero tal cual subido.
app.mount("/media", StaticFiles(directory=settings.MEDIA_DIR), name="media")


@app.get("/health")
def health_check():
    return {"status": "ok"}
