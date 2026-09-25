"""Avatar, banner y portada: validar, re-codificar y guardar.

Nada de lo que sube el usuario se sirve tal cual. Se abre con Pillow (el tipo lo decide
el contenido, no la extensión ni el Content-Type), se decodifica y se vuelve a codificar
en WebP desde los píxeles. Así no viaja el EXIF (con la ubicación GPS de la foto), ni
metadatos, ni bytes extra pegados detrás de una imagen válida.
"""
from __future__ import annotations

import secrets
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.core.database import Base

ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP")
# 24 Mpx cubre la foto de cualquier móvil y deja fuera las bombas de descompresión: un
# PNG de 4 MB puede declarar 50.000 x 50.000 píxeles y pedir gigas al decodificarlo.
MAX_PIXELS = 24_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
WEBP_QUALITY = 85


@dataclass(frozen=True)
class ImageKind:
    field: str  # columna de la fila dueña de la imagen
    subdir: str
    max_side: int
    setting_name: str  # el `*_MAX_BYTES` de `settings` que la limita

    @property
    def max_bytes(self) -> int:
        # Leído en cada uso y no al importar: los tests cambian los límites.
        return getattr(settings, self.setting_name)


AVATAR = ImageKind(field="avatar_path", subdir="avatars", max_side=512, setting_name="AVATAR_MAX_BYTES")
BANNER = ImageKind(field="banner_path", subdir="banners", max_side=1600, setting_name="BANNER_MAX_BYTES")
# Apaisada, pero sin recortar: 1280 por lado y que el CSS decida la proporción. Recortar
# aquí perdería píxeles que el frontend podría querer en otra maqueta.
COVER = ImageKind(field="cover_path", subdir="covers", max_side=1280, setting_name="COVER_MAX_BYTES")


class ImageRejectedError(Exception):
    """La imagen no se acepta. El mensaje es apto para mostrarse."""

    def __init__(self, message: str, status_code: int = 422, code: str = "image_invalid"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def media_root() -> Path:
    return Path(settings.MEDIA_DIR).resolve()


def url_for(path: str | None) -> str | None:
    return f"{settings.MEDIA_BASE_URL.rstrip('/')}/media/{path}" if path else None


def reencode(data: bytes, max_side: int) -> bytes:
    """Imagen JPEG/PNG/WebP válida -> WebP limpio de como mucho `max_side` por lado."""
    invalid = ImageRejectedError("El archivo no es una imagen JPEG, PNG o WebP válida.", 415, "image_type")
    with warnings.catch_warnings():
        # Pillow solo avisa entre 1x y 2x el límite; aquí un aviso ya es un rechazo.
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            # `formats` limita los decodificadores que se prueban: un GIF, un SVG o un PSD
            # renombrados a .png no llegan a ningún parser.
            with Image.open(BytesIO(data), formats=ALLOWED_FORMATS) as probe:
                if probe.width * probe.height > MAX_PIXELS:
                    raise ImageRejectedError("La imagen tiene demasiados píxeles.", 422, "image_too_large")
                probe.verify()
            with Image.open(BytesIO(data), formats=ALLOWED_FORMATS) as img:
                if img.format == "JPEG":
                    # Decodifica ya reducida: una foto de 12 Mpx no ocupa 12 Mpx en memoria.
                    img.draft("RGB", (max_side, max_side))
                # Aplica la orientación del EXIF antes de tirarlo, o la foto sale girada.
                img = ImageOps.exif_transpose(img)
                has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
                img = img.convert("RGBA" if has_alpha else "RGB")
                img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                img.info = {}
                out = BytesIO()
                img.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
                return out.getvalue()
        except ImageRejectedError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ImageRejectedError("La imagen tiene demasiados píxeles.", 422, "image_too_large")
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError):
            raise invalid


def delete_file(relative: str | None) -> None:
    if not relative:
        return
    root = media_root()
    target = (root / relative).resolve()
    # La ruta sale de la base, pero borrar fuera de MEDIA_DIR no debe poder pasar nunca.
    if root not in target.parents:
        return
    target.unlink(missing_ok=True)


def store(db: DbSession, owner: Base, kind: ImageKind, data: bytes) -> None:
    """`owner` es cualquier fila con la columna `kind.field`: `User` (avatar, banner) o
    `StoryBlueprint` (portada). El tipo no puede ser más concreto porque el nombre de la
    columna lo pone `kind`, no la clase."""
    encoded = reencode(data, kind.max_side)
    folder = media_root() / kind.subdir
    folder.mkdir(parents=True, exist_ok=True)
    # Nombre aleatorio: no se puede adivinar la imagen de nadie ni enumerarlas, y cada
    # versión tiene URL propia, así que una caché nunca sirve la anterior.
    relative = f"{kind.subdir}/{secrets.token_urlsafe(18)}.webp"
    (media_root() / relative).write_bytes(encoded)

    previous = getattr(owner, kind.field)
    setattr(owner, kind.field, relative)
    try:
        db.commit()
    except Exception:
        db.rollback()
        delete_file(relative)
        raise
    # Después del commit: si fallara, la fila seguiría apuntando al fichero anterior.
    delete_file(previous)


def remove(db: DbSession, owner: Base, kind: ImageKind) -> None:
    previous = getattr(owner, kind.field)
    if previous is None:
        return
    setattr(owner, kind.field, None)
    db.commit()
    delete_file(previous)
