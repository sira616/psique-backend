import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.security import get_current_user
from app.models.story import STORY_ACTIVE
from app.models.user import User
from app.schemas.story import CharacterOut, ChatIn, StoryCreateIn, StoryOut, StorySummaryOut
from app.services import chat_stream_service, economy_service, story_service
from app.story import state_machine as sm

router = APIRouter(prefix="/api", tags=["stories"])

# Cada mensaje son dos llamadas al LLM que pagamos nosotros. Más holgado que el login
# porque conversar es el uso normal.
_chat_rate_limiter = RateLimiter(
    settings.RATE_LIMIT_MAX_REQUESTS * 4, settings.RATE_LIMIT_WINDOW_SECONDS
)


CHAPTER_LOCKED = "Este capítulo está bloqueado. Desbloquéalo para seguir la historia."
STORY_ARCHIVED = "Esta partida está archivada: se puede leer, pero no continuar."
NOT_ENOUGH_TO_READ = "Te faltan óbolos para empezar este libro. Puedes ganar más en Rasca y gana."


def story_archived_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT, content={"detail": STORY_ARCHIVED, "code": "story_archived"}
    )


def _sse(event: dict) -> bytes:
    data = json.dumps(event["data"], ensure_ascii=False, default=str)
    return f"event: {event['event']}\ndata: {data}\n\n".encode("utf-8")


@router.get("/characters", response_model=list[CharacterOut])
def list_characters(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return story_service.list_characters(db, user)


@router.get("/stories", response_model=list[StorySummaryOut])
def list_stories(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return story_service.list_stories(db, user)


@router.post("/stories", response_model=StoryOut, status_code=status.HTTP_201_CREATED)
def create_story(
    payload: StoryCreateIn,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Empieza un libro o devuelve la partida activa que ya había (200, sin cobrar).

    La primera partida de un libro con `free_first_read` es gratis; cualquier otra cuesta
    `READ_COST`. Visibilidad antes que idempotencia: con un libro despublicado da 404 aunque
    quede una partida activa, que se sigue abriendo por su id.
    """
    # Uno propio de otra cuenta da el mismo 404 que uno que no existe.
    profile = story_service.resolve_profile(db, user.id, payload.characterId)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ese personaje no existe.")
    try:
        story, created = story_service.start_story(db, user, payload.characterId, profile)
    except economy_service.InsufficientObolosError:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=NOT_ENOUGH_TO_READ)
    if not created:
        response.status_code = status.HTTP_200_OK
    return story_service.story_out(db, story)


def _owned(db: Session, user: User, story_id: str):
    story = story_service.get_owned_story(db, user, story_id)
    if story is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Historia no encontrada.")
    return story


@router.get("/stories/{story_id}", response_model=StoryOut)
def get_story(story_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return story_service.story_out(db, _owned(db, user, story_id))


@router.post("/stories/{story_id}/chat", dependencies=[Depends(_chat_rate_limiter)])
def chat(
    story_id: str,
    payload: ChatIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    story = _owned(db, user, story_id)
    # Antes de abrir el stream y de validar nada más: una historia congelada no guarda el
    # mensaje, no llama al LLM y no mueve afinidad, turnos ni memoria.
    if story.status != STORY_ACTIVE:
        return story_archived_response()
    if story.pending_phase is not None:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": CHAPTER_LOCKED,
                "code": "chapter_locked",
                "state": story_service.state_out(story).model_dump(mode="json"),
            },
        )

    choice = None
    if payload.choiceId is not None:
        choice = sm.find_choice(sm.Phase(story.phase), payload.choiceId)
        if choice is None:
            # Se valida antes de abrir el stream: un 422 limpio es mejor que un error a
            # mitad de SSE, y una sugerencia de otra fase no puntúa en esta.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Esa opción no está disponible en esta fase.",
            )
    text = choice.message if choice else payload.message.strip()

    def event_source() -> Iterator[bytes]:
        for event in chat_stream_service.stream_turn(story.id, text, choice):
            yield _sse(event)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/stories/{story_id}/unlock-chapter")
def unlock_chapter(story_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Paga el capítulo pendiente. Devuelve lo mismo que el evento SSE `state` más el saldo."""
    story = _owned(db, user, story_id)
    try:
        transition, balance = story_service.unlock_chapter(db, user, story)
    except story_service.StoryArchivedError:
        return story_archived_response()
    except story_service.ChapterNotLockedError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No hay ningún capítulo por desbloquear.")
    except economy_service.InsufficientObolosError:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Te faltan óbolos para desbloquear este capítulo. Puedes ganar más en Rasca y gana.",
        )
    return story_service.state_payload(story, transition) | {"balance": balance}
