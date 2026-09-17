from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.security import get_current_user
from app.llm import router as llm_router
from app.models.user import User
from app.schemas.custom_story import CustomStoryIn, CustomStoryOut, CustomStoryPatchIn
from app.services import custom_story_service as svc

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


@router.patch("/{blueprint_id}", response_model=CustomStoryOut)
def update_custom_story(
    blueprint_id: str,
    payload: CustomStoryPatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Publicar o despublicar y decidir si la primera lectura es gratis. Despublicar la
    saca de Explorar y del perfil, pero las partidas que otras cuentas ya empezaron siguen
    jugables."""
    blueprint = svc.get_owned(db, user.id, blueprint_id)
    if blueprint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return svc.to_out(
        svc.update_settings(
            db, blueprint, is_public=payload.isPublic, free_first_read=payload.freeFirstRead, adult=payload.adult
        )
    )


@router.delete("/{blueprint_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_custom_story(
    blueprint_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    blueprint = svc.get_owned(db, user.id, blueprint_id)
    if blueprint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    svc.delete_owned(db, blueprint)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
