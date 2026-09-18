"""Libros: la ficha pública de un predefinido o de una historia propia.

"Libro" es lo que se lee (`characterId`); "partida" es cada lectura (`Story`). Todo lo de
aquí mira la visibilidad primero: un libro privado ajeno o borrado es, para quien
pregunta, uno que no existe.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.models.story import STORY_ARCHIVED, STORY_CLOSED, BookReview, Story, StoryBlueprint
from app.models.user import User
from app.schemas.book import (
    BookCardOut,
    BookOut,
    BookProgressOut,
    BookStatsOut,
    BookViewerOut,
    HistoryItemOut,
    PrimaryActionOut,
    ReviewIn,
    ReviewOut,
)
from app.schemas.profile import AuthorOut
from app.services import conduct_service, custom_story_service, media_service, story_service
from app.story import state_machine as sm
from app.story.character_profile import get_character, load_characters
from app.story.moderation import check_user_text

RECOMMENDED_LIMIT = 6


@dataclass(frozen=True)
class Book:
    id: str
    origin: str
    mode: str | None
    title: str
    hook: str
    character_name: str | None
    tone: str | None
    owner: User | None
    is_public: bool
    free_first_read: bool
    published_at: datetime | None
    created_at: datetime | None
    adult: bool = False


def author_out(user: User) -> AuthorOut:
    return AuthorOut(
        displayName=user.display_name, handle=user.handle, avatarUrl=media_service.url_for(user.avatar_path)
    )


def _predefined(book_id: str) -> Book | None:
    profile = get_character(book_id)
    if profile is None:
        return None
    return Book(
        id=book_id, origin="psique", mode=None, title=profile.nombre, hook=profile.tagline,
        character_name=profile.nombre, tone=profile.tono, owner=None, is_public=True,
        free_first_read=profile.free_first_read, published_at=None, created_at=None, adult=profile.adult,
    )


def _from_blueprint(blueprint: StoryBlueprint, owner: User) -> Book:
    profile = custom_story_service.profile_of(blueprint)
    return Book(
        id=custom_story_service.character_ref(blueprint.id),
        origin="propia",
        mode=blueprint.mode,
        title=blueprint.title,
        hook=blueprint.hook,
        # En concepto el nombre lo inventó el LLM y se descubre jugando.
        character_name=profile.nombre if blueprint.mode == "definida" else None,
        tone=profile.tono,
        owner=owner,
        is_public=blueprint.is_public,
        free_first_read=blueprint.free_first_read,
        published_at=blueprint.published_at,
        created_at=blueprint.created_at,
        adult=blueprint.adult,
    )


def get_visible(db: DbSession, viewer: User, book_id: str) -> Book | None:
    """Predefinido, o propio sin borrar que sea público o de quien mira."""
    if not book_id.startswith(custom_story_service.CUSTOM_PREFIX):
        return _predefined(book_id)
    blueprint = custom_story_service.blueprint_for(db, book_id)
    if blueprint is None or blueprint.deleted_at is not None:
        return None
    if not blueprint.is_public and blueprint.owner_id != viewer.id:
        return None
    return _from_blueprint(blueprint, db.get(User, blueprint.owner_id))


def is_author(book: Book, user: User) -> bool:
    return book.owner is not None and book.owner.id == user.id


def _readers(db: DbSession, book_ids: list[str] | None = None) -> dict[str, int]:
    query = select(Story.character_id, func.count(distinct(Story.user_id))).group_by(Story.character_id)
    if book_ids is not None:
        query = query.where(Story.character_id.in_(book_ids))
    return dict(db.execute(query).all())


# --- Ficha -------------------------------------------------------------------------------


def _progress(story: Story) -> BookProgressOut:
    phase = sm.Phase(story.phase)
    return BookProgressOut(
        phase=phase.value,
        phaseLabel=sm.PHASE_LABELS[phase],
        phaseIndex=sm.PHASE_ORDER.index(phase),
        phaseCount=len(sm.PHASE_ORDER),
        affinity=story.affinity,
        chapterLocked=story.pending_phase is not None,
    )


def review_out(review: BookReview, author: User, viewer: User) -> ReviewOut:
    return ReviewOut(
        id=review.id,
        rating=review.rating,
        text=review.text,
        author=author_out(author),
        isMine=review.user_id == viewer.id,
        createdAt=review.created_at,
        updatedAt=review.updated_at,
    )


def book_out(db: DbSession, viewer: User, book: Book) -> BookOut:
    rating_avg, review_count = db.execute(
        select(func.avg(BookReview.rating), func.count(BookReview.id)).where(BookReview.book_id == book.id)
    ).one()

    active = story_service.active_story(db, viewer.id, book.id)
    has_read = story_service.has_read(db, viewer.id, book.id)
    if active is None:
        status = "sin_empezar"
    elif active.phase == sm.PHASE_ORDER[-1].value and active.pending_phase is None:
        status = "leido"
    else:
        status = "leyendo"
    if active is not None:
        action = PrimaryActionOut(kind="continuar", cost=0)
    else:
        action = PrimaryActionOut(kind="leer", cost=story_service.start_cost(db, viewer.id, book.id))

    mine = db.scalar(select(BookReview).where(BookReview.book_id == book.id, BookReview.user_id == viewer.id))
    return BookOut(
        id=book.id,
        origin=book.origin,
        mode=book.mode,
        title=book.title,
        hook=book.hook,
        characterName=book.character_name,
        tone=book.tone,
        author=author_out(book.owner) if book.owner else None,
        isMine=is_author(book, viewer),
        isPublic=book.is_public,
        chapterCount=len(sm.PHASE_ORDER),
        freeFirstRead=book.free_first_read,
        adult=book.adult,
        readCost=settings.READ_COST,
        publishedAt=book.published_at,
        createdAt=book.created_at,
        stats=BookStatsOut(
            readers=_readers(db, [book.id]).get(book.id, 0),
            ratingAverage=round(float(rating_avg), 1) if rating_avg is not None else None,
            reviewCount=review_count,
        ),
        viewer=BookViewerOut(
            status=status,
            activeStoryId=active.id if active else None,
            progress=_progress(active) if active else None,
            primaryAction=action,
            canReread=has_read,
            rereadCost=settings.READ_COST,
            canReview=has_read and not is_author(book, viewer),
            myReview=review_out(mine, viewer, viewer) if mine else None,
            adultRequired=book.adult and not conduct_service.adult_confirmed(viewer),
        ),
    )


def history(db: DbSession, viewer: User, book: Book) -> list[HistoryItemOut]:
    """Partidas archivadas y cerradas de quien pregunta: el historial de otra cuenta no sale."""
    stories = db.scalars(
        select(Story)
        .where(
            Story.user_id == viewer.id,
            Story.character_id == book.id,
            Story.status.in_((STORY_ARCHIVED, STORY_CLOSED)),
        )
        .order_by(func.coalesce(Story.archived_at, Story.closed_at).desc(), Story.created_at.desc(), Story.id)
    )
    items = []
    for story in stories:
        progress = _progress(story)
        items.append(
            HistoryItemOut(
                storyId=story.id,
                status=story.status,
                startedAt=story.created_at,
                archivedAt=story.archived_at,
                closedAt=story.closed_at,
                phase=progress.phase,
                phaseLabel=progress.phaseLabel,
                phaseIndex=progress.phaseIndex,
                phaseCount=progress.phaseCount,
                affinity=story.affinity,
            )
        )
    return items


# --- Recomendados ------------------------------------------------------------------------


def _norm(text: str | None) -> str:
    folded = unicodedata.normalize("NFKD", (text or "").strip().casefold())
    return "".join(c for c in folded if not unicodedata.combining(c))


def recommended(db: DbSession, book: Book, viewer: User) -> list[BookCardOut]:
    """Hasta seis libros parecidos, solo predefinidos o públicos sin borrar (tampoco los
    privados de quien mira: la lista es la misma para todos).

    Puntuación: +2 mismo tono (sin mayúsculas ni acentos, solo si los dos lo tienen), +1
    mismo origen y modo, +1 mismo autor. Orden: puntuación desc, lectores desc, título.
    Se puntúan todos los candidatos en memoria: con el volumen actual es más simple que
    un ranking en SQL.
    """
    candidates = [b for b in (_predefined(pid) for pid in load_characters()) if b is not None]
    rows = db.execute(
        select(StoryBlueprint, User)
        .join(User, User.id == StoryBlueprint.owner_id)
        .where(StoryBlueprint.is_public.is_(True), StoryBlueprint.deleted_at.is_(None))
    ).all()
    candidates += [_from_blueprint(b, u) for b, u in rows]
    candidates = [c for c in candidates if c.id != book.id]
    if not conduct_service.adult_confirmed(viewer):
        candidates = [c for c in candidates if not c.adult]
    readers = _readers(db)

    tone = _norm(book.tone)

    def score(c: Book) -> int:
        points = 0
        if tone and _norm(c.tone) == tone:
            points += 2
        if (c.origin, c.mode) == (book.origin, book.mode):
            points += 1
        if book.owner is not None and c.owner is not None and c.owner.id == book.owner.id:
            points += 1
        return points

    candidates.sort(key=lambda c: (-score(c), -readers.get(c.id, 0), c.title.casefold()))
    return [
        BookCardOut(
            id=c.id, origin=c.origin, mode=c.mode, title=c.title, hook=c.hook, tone=c.tone,
            author=author_out(c.owner) if c.owner else None, readers=readers.get(c.id, 0), adult=c.adult,
        )
        for c in candidates[:RECOMMENDED_LIMIT]
    ]


# --- Reseñas -----------------------------------------------------------------------------


class ReviewForbiddenError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class ReviewContentError(Exception):
    pass


NOT_STARTED = "Solo puedes reseñar libros que hayas empezado."
OWN_BOOK = "No puedes reseñar tu propio libro."


def list_reviews(db: DbSession, viewer: User, book: Book, *, limit: int, offset: int) -> tuple[list[ReviewOut], bool]:
    rows = db.execute(
        select(BookReview, User)
        .join(User, User.id == BookReview.user_id)
        .where(BookReview.book_id == book.id)
        .order_by(BookReview.created_at.desc(), BookReview.id.desc())
        .limit(limit + 1)
        .offset(offset)
    ).all()
    return [review_out(r, u, viewer) for r, u in rows[:limit]], len(rows) > limit


def upsert_review(db: DbSession, viewer: User, book: Book, payload: ReviewIn) -> tuple[ReviewOut, bool]:
    """Crea o edita la reseña de quien mira. El bool dice si se creó."""
    if is_author(book, viewer):
        raise ReviewForbiddenError("own_book", OWN_BOOK)
    if not story_service.has_read(db, viewer.id, book.id):
        raise ReviewForbiddenError("not_started", NOT_STARTED)
    rejection = check_user_text(payload.text)
    if rejection:
        raise ReviewContentError(rejection)

    def current() -> BookReview | None:
        return db.scalar(select(BookReview).where(BookReview.book_id == book.id, BookReview.user_id == viewer.id))

    review, created = current(), False
    if review is None:
        review, created = BookReview(user_id=viewer.id, book_id=book.id, rating=payload.rating, text=payload.text), True
        db.add(review)
        try:
            db.commit()
        except IntegrityError:
            # Dos envíos a la vez: el segundo edita la que creó el primero.
            db.rollback()
            review, created = current(), False
    if not created:
        review.rating = payload.rating
        review.text = payload.text
        db.commit()
    db.refresh(review)
    return review_out(review, viewer, viewer), created


def delete_review(db: DbSession, viewer: User, book: Book) -> bool:
    review = db.scalar(select(BookReview).where(BookReview.book_id == book.id, BookReview.user_id == viewer.id))
    if review is None:
        return False
    db.delete(review)
    db.commit()
    return True
