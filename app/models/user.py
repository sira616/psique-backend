from __future__ import annotations

from datetime import datetime

from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, false, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class User(Base):
    """Cuenta local. La identidad es el nombre de usuario: no hace falta un email para
    leer una historia, y un dato que no se pide es un dato que no se puede filtrar."""

    __tablename__ = "users"
    # La última defensa contra un saldo negativo: aunque un gasto se saltara el UPDATE
    # condicional de `economy_service.spend`, la base lo rechaza.
    __table_args__ = (CheckConstraint("obolos >= 0", name="ck_users_obolos_no_negativo"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # En minúsculas: "Lucia" y "lucia" no pueden ser dos cuentas.
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(64))
    # Cuentas de desarrollo: herramientas internas futuras. Solo se fija por script, nunca
    # desde la API.
    is_dev: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Confirmación explícita de mayoría de edad para abrir libros +18. Es una declaración,
    # no una verificación: no basta para subir el techo de contenido.
    adult_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Reincidencia en cierres por la política de contenido: hasta esta fecha no se empieza,
    # continúa ni relee ninguna partida. Ver `conduct_service`.
    restricted_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Caché del saldo: la verdad está en `obolo_movements`. Se toca solo desde
    # `app.services.economy_service`, en la misma transacción que su movimiento.
    obolos: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # --- Perfil público ---
    # En la misma tabla y no en `profiles`: es 1:1 obligatorio y leerlo nunca cuesta un
    # join. El handle es la identidad pública y se puede cambiar; `username` es el login y
    # no sale nunca en un perfil, así cambiar uno no toca el otro.
    handle: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    bio: Mapped[Optional[str]] = mapped_column(String(280), nullable=True)
    link: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # Rutas relativas a MEDIA_DIR, no URLs: si cambia dónde se sirven, la base no se toca.
    avatar_path: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    banner_path: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    show_published: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    show_reading: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
