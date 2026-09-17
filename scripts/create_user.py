"""Crear una cuenta desde consola, con la marca de dev si hace falta.

    .\venv\Scripts\python.exe -m scripts.create_user syreta --dev
    .\venv\Scripts\python.exe -m scripts.create_user demo --password DemoPsique2026

Sin --password genera una contraseña aleatoria y la imprime una sola vez. `is_dev` no se
puede fijar desde la API a propósito: este script es la única puerta.
"""
from __future__ import annotations

import argparse
import secrets
import sys

# La consola de Windows no es UTF-8 por defecto y los acentos salían rotos.
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.migrations import upgrade_to_head
from app.core.passwords import PasswordPolicyError
from app.models.user import User
from app.services import auth_service
from app.services.auth_service import AuthError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("username")
    parser.add_argument("--password", help="Si falta, se genera una aleatoria.")
    parser.add_argument("--display-name")
    parser.add_argument("--dev", action="store_true", help="Marca la cuenta como de desarrollo.")
    args = parser.parse_args()

    # Aa1 garantiza la variedad que pide la política aunque el azar no la traiga.
    password = args.password or f"Aa1{secrets.token_urlsafe(18)}"
    upgrade_to_head()
    with SessionLocal() as db:
        try:
            user = auth_service.create_user(db, args.username, password, args.display_name)
        except (AuthError, PasswordPolicyError) as exc:
            print(f"No se creó {args.username}: {exc}", file=sys.stderr)
            return 1
        if args.dev:
            user.is_dev = True
            db.commit()
        dev = " (dev)" if db.scalar(select(User.is_dev).where(User.id == user.id)) else ""
        print(f"Creado {user.username}{dev}")
        if not args.password:
            print(f"Contraseña: {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
