"""Limpieza de lo que ya no usa nadie. Por defecto solo informa; con --apply borra.

    ./venv/Scripts/python.exe -m scripts.cleanup            # dry-run: qué borraría
    ./venv/Scripts/python.exe -m scripts.cleanup --apply    # lo borra

- Historias propias con `deleted_at` que ya no juega ninguna otra cuenta (con sus reseñas
  y las partidas que le quedaran a su autor).
- Ficheros de `MEDIA_DIR` que no referencia ninguna cuenta. Los de menos de una hora se
  respetan: `media_service.store` escribe el fichero antes del commit que lo enlaza.
- Refresh tokens caducados o revocados hace más de `TOKEN_GRACE_DAYS`.
- Ventanas caducadas del rate limiter.

No migra la base: tiene que estar ya en la última versión (el backend migra al arrancar).
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.models.auth import RefreshToken
from app.models.limits import RateLimitWindow
from app.models.story import BookReview, ConductIncident, MemoryFact, Message, Story, StoryBlueprint, StoryEvent
from app.models.user import User
from app.services import custom_story_service
from app.services.media_service import AVATAR, BANNER

TOKEN_GRACE_DAYS = 7
MEDIA_MIN_AGE_SECONDS = 3600


@dataclass
class Report:
    blueprints: list[str] = field(default_factory=list)
    media_files: list[str] = field(default_factory=list)
    refresh_tokens: int = 0
    rate_limit_windows: int = 0

    def lines(self, applied: bool) -> list[str]:
        verb = "Borrado" if applied else "Se borraría"
        out = [
            f"{verb}: {len(self.blueprints)} historia(s) propia(s) borrada(s) sin partidas ajenas",
            *(f"  - {b}" for b in self.blueprints),
            f"{verb}: {len(self.media_files)} fichero(s) huérfano(s) de media",
            *(f"  - {m}" for m in self.media_files),
            f"{verb}: {self.refresh_tokens} refresh token(s) caducado(s) o revocado(s) antiguo(s)",
            f"{verb}: {self.rate_limit_windows} ventana(s) caducada(s) del rate limiter",
        ]
        return out


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def orphan_blueprints(db: DbSession) -> list[StoryBlueprint]:
    orphans = []
    for blueprint in db.scalars(select(StoryBlueprint).where(StoryBlueprint.deleted_at.is_not(None))):
        ref = custom_story_service.character_ref(blueprint.id)
        foreign = select(func.count()).select_from(Story).where(Story.character_id == ref)
        if blueprint.owner_id is not None:
            foreign = foreign.where(Story.user_id != blueprint.owner_id)
        if not db.scalar(foreign):
            orphans.append(blueprint)
    return orphans


def orphan_media(db: DbSession, media_root: Path, now_epoch: float) -> list[Path]:
    if not media_root.is_dir():
        return []
    referenced = set()
    for avatar, banner in db.execute(select(User.avatar_path, User.banner_path)):
        referenced.update(p for p in (avatar, banner) if p)
    orphans = []
    # Solo las carpetas que escribe `media_service`: cualquier otra cosa en MEDIA_DIR no es suya.
    candidates = (p for kind in (AVATAR, BANNER) for p in (media_root / kind.subdir).rglob("*"))
    for path in sorted(candidates):
        if not path.is_file():
            continue
        relative = path.relative_to(media_root).as_posix()
        if relative in referenced or now_epoch - path.stat().st_mtime < MEDIA_MIN_AGE_SECONDS:
            continue
        orphans.append(path)
    return orphans


def _token_filter(now: datetime):
    cutoff = now - timedelta(days=TOKEN_GRACE_DAYS)
    return or_(RefreshToken.expires_at < cutoff, and_(RefreshToken.revoked.is_(True), RefreshToken.issued_at < cutoff))


def run(db: DbSession, media_root: Path, *, apply: bool, now: datetime | None = None, now_epoch: float | None = None) -> Report:
    now = now or _now()
    now_epoch = time.time() if now_epoch is None else now_epoch
    media_root = media_root.resolve()

    blueprints = orphan_blueprints(db)
    media = orphan_media(db, media_root, now_epoch)
    report = Report(
        blueprints=[f"{b.id} «{b.title}»" for b in blueprints],
        media_files=[p.relative_to(media_root).as_posix() for p in media],
        refresh_tokens=db.scalar(select(func.count()).select_from(RefreshToken).where(_token_filter(now))) or 0,
        rate_limit_windows=db.scalar(
            select(func.count()).select_from(RateLimitWindow).where(RateLimitWindow.expires_at <= int(now_epoch))
        ) or 0,
    )
    if not apply:
        return report

    for blueprint in blueprints:
        ref = custom_story_service.character_ref(blueprint.id)
        # Las del autor, si le quedaba alguna: sin perfil no se podrían ni abrir.
        leftover = select(Story.id).where(Story.character_id == ref)
        db.execute(delete(ConductIncident).where(ConductIncident.story_id.in_(leftover)))
        for model in (Message, MemoryFact, StoryEvent):
            db.execute(delete(model).where(model.story_id.in_(leftover)))
        db.execute(delete(Story).where(Story.character_id == ref))
        db.execute(delete(BookReview).where(BookReview.book_id == ref))
        db.execute(delete(StoryBlueprint).where(StoryBlueprint.id == blueprint.id))
    db.execute(delete(RefreshToken).where(_token_filter(now)))
    db.execute(delete(RateLimitWindow).where(RateLimitWindow.expires_at <= int(now_epoch)))
    db.commit()
    # Los ficheros al final: si el commit fallara, no se habría perdido nada referenciado.
    for path in media:
        path.unlink(missing_ok=True)
    return report


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="Borra de verdad (sin esto es un dry-run).")
    args = parser.parse_args(argv)

    from app.core.config import settings
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        report = run(db, Path(settings.MEDIA_DIR), apply=args.apply)
    print("Limpieza aplicada." if args.apply else "Dry-run: no se ha borrado nada (usa --apply para borrar).")
    print("\n".join(report.lines(args.apply)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
