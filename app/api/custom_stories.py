from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api._uploads import UPLOAD_DOC, file_error, read_upload, upload_rate_limiter
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.security import get_current_user
from app.llm import router as llm_router
from app.models.story import StoryBlueprint
from app.models.user import User
from app.schemas.book import CustomStoryStatsOut
from app.schemas.custom_story import CustomStoryIn, CustomStoryOut, CustomStoryPatchIn
from app.services import book_service, media_service
from app.services import custom_story_service as svc
from app.services.media_service import COVER, ImageRejectedError

router = APIRouter(prefix="/api/custom-stories", tags=["custom-stories"])

# Crear en modo concepto es una llamada larga al LLM (dos con el reintento).
_create_rate_limiter = RateLimiter(settings.RATE_LIMIT_MAX_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS)

NOT_FOUND = "Historia propia no encontrada."
LLM_UNAVAILABLE = (
    "Ahora mismo no se puede crear la historia: el modelo de lenguaje no está disponible. "
    "Inténtalo de nuevo en un momento."
)
LLM_REFUSAL = "Esa idea no se puede convertir en una historia de Psique. Prueba con otra."


@router.post(
    "",
    response_model=CustomStoryOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_create_rate_limiter)],
)
def create_custom_story(
    payload: Annotated[CustomStoryIn, Body()],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        blueprint = svc.create(db, user, payload)
    except svc.ContentRejectedError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except svc.BlueprintLimitError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except svc.ConceptGenerationError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=svc.GENERATION_FAILED_MESSAGE)
    except llm_router.LLMRefusalError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=LLM_REFUSAL)
    except llm_router.LLMUnavailableError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=LLM_UNAVAILABLE)
    return svc.to_out(blueprint)


@router.get("", response_model=list[CustomStoryOut])
def list_custom_stories(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return [svc.to_out(b) for b in svc.list_owned(db, user)]


def _owned(db: Session, user: User, blueprint_id: str) -> StoryBlueprint:
    blueprint = svc.get_owned(db, user.id, blueprint_id)
    if blueprint is None:
        # La de otra cuenta, para quien pregunta, no existe: mismo 404 que si no estuviera.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return blueprint


@router.get("/{blueprint_id}", response_model=CustomStoryOut)
def get_custom_story(
    blueprint_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    return svc.to_out(_owned(db, user, blueprint_id))


@router.get("/{blueprint_id}/stats", response_model=CustomStoryStatsOut)
def get_custom_story_stats(
    blueprint_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Lectores, partidas en curso y reseñas de una historia propia, solo para su autor."""
    return book_service.blueprint_stats(db, user, _owned(db, user, blueprint_id))


@router.patch("/{blueprint_id}", response_model=CustomStoryOut)
def update_custom_story(
    blueprint_id: str,
    payload: CustomStoryPatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Edita la ficha de una historia propia: texto, visibilidad y, en modo definida, el
    personaje. Despublicar la saca de Explorar y del perfil, pero las partidas que otras
    cuentas ya empezaron siguen jugables."""
    blueprint = _owned(db, user, blueprint_id)
    try:
        return svc.to_out(svc.update_owned(db, blueprint, payload))
    except (svc.ContentRejectedError, svc.ProfileNotEditableError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post(
    "/{blueprint_id}/cover",
    response_model=CustomStoryOut,
    openapi_extra=UPLOAD_DOC,
    dependencies=[Depends(upload_rate_limiter)],
)
async def upload_cover(
    blueprint_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    blueprint = _owned(db, user, blueprint_id)
    data = await read_upload(request, COVER.max_bytes)
    try:
        # Decodificar y re-codificar bloquea: fuera del bucle de eventos.
        await run_in_threadpool(media_service.store, db, blueprint, COVER, data)
    except ImageRejectedError as exc:
        raise file_error(exc.status_code, str(exc), exc.code)
    return svc.to_out(blueprint)


@router.delete("/{blueprint_id}/cover", status_code=status.HTTP_204_NO_CONTENT)
def delete_cover(
    blueprint_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    media_service.remove(db, _owned(db, user, blueprint_id), COVER)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{blueprint_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_custom_story(
    blueprint_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    svc.delete_owned(db, _owned(db, user, blueprint_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
