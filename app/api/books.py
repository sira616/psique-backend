from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.book import BookCardOut, BookOut, HistoryItemOut, ReviewIn, ReviewOut, ReviewPageOut
from app.schemas.story import StoryOut
from app.services import book_service, economy_service, story_service
from app.services.book_service import Book
from app.services.profile_service import field_error

router = APIRouter(prefix="/api/books", tags=["books"])

BOOK_NOT_FOUND = "No encontramos este libro."
NOT_STARTED = "Aún no has empezado este libro."
NOT_ENOUGH_TO_REREAD = "Te faltan óbolos para empezar este libro. Puedes ganar más en Rasca y gana."

# `custom:<hex32>` son 39 caracteres; la columna admite 40.
BookId = Path(min_length=1, max_length=40)


def _book(db: Session, user: User, book_id: str) -> Book:
    book = book_service.get_visible(db, user, book_id)
    if book is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=BOOK_NOT_FOUND)
    return book


@router.get("/{book_id}", response_model=BookOut)
def get_book(book_id: str = BookId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return book_service.book_out(db, user, _book(db, user, book_id))


@router.get("/{book_id}/history", response_model=list[HistoryItemOut])
def get_history(book_id: str = BookId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return book_service.history(db, user, _book(db, user, book_id))


@router.get("/{book_id}/recommended", response_model=list[BookCardOut])
def get_recommended(book_id: str = BookId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return book_service.recommended(db, _book(db, user, book_id))


@router.post("/{book_id}/reread", response_model=StoryOut, status_code=status.HTTP_201_CREATED)
def reread(book_id: str = BookId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Archiva la partida activa y empieza otra. Cuesta `READ_COST` siempre, también si la
    primera lectura fue gratis."""
    book = _book(db, user, book_id)
    profile = story_service.resolve_profile(db, user.id, book.id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=BOOK_NOT_FOUND)
    try:
        story = story_service.reread(db, user, book.id, profile)
    except story_service.NotStartedError:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": NOT_STARTED, "code": "not_started"}
        )
    except economy_service.InsufficientObolosError:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=NOT_ENOUGH_TO_REREAD)
    except IntegrityError:
        # Otra relectura simultánea ganó: esta no cobró ni archivó nada.
        active = story_service.active_story(db, user.id, book.id)
        if active is None:
            raise
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=story_service.story_out(db, active).model_dump(mode="json"),
        )
    return story_service.story_out(db, story)


@router.get("/{book_id}/reviews", response_model=ReviewPageOut)
def list_reviews(
    book_id: str = BookId,
    limit: int = Query(default=10, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items, has_more = book_service.list_reviews(db, user, _book(db, user, book_id), limit=limit, offset=offset)
    return ReviewPageOut(items=items, limit=limit, offset=offset, nextOffset=offset + limit if has_more else None)


@router.put("/{book_id}/reviews/me", response_model=ReviewOut, status_code=status.HTTP_201_CREATED)
def put_my_review(
    payload: ReviewIn,
    response: Response,
    book_id: str = BookId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    book = _book(db, user, book_id)
    try:
        review, created = book_service.upsert_review(db, user, book, payload)
    except book_service.ReviewForbiddenError as exc:
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": exc.detail, "code": exc.code})
    except book_service.ReviewContentError as exc:
        # Misma forma que los errores de campo del perfil.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[field_error("text", str(exc), "content_policy")],
        )
    if not created:
        response.status_code = status.HTTP_200_OK
    return review


@router.delete("/{book_id}/reviews/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_review(book_id: str = BookId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not book_service.delete_review(db, user, _book(db, user, book_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No has reseñado este libro.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
