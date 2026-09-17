"""Contraseñas: política, hash y verificación.

Argon2id y no bcrypt: es lo que recomienda OWASP hoy, no tiene el tope de 72 bytes de
bcrypt (que trunca en silencio) y la librería trae parámetros por defecto sensatos.

La política pide longitud y variedad, pero **no carácter especial**. La evidencia de
usabilidad es bastante clara en que exigirlo empuja a la gente a `Contraseña1!` y
patrones equivalentes, que son peores que una contraseña larga sin símbolos. La
longitud mínima es lo que de verdad hace el trabajo.
"""
from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError

MIN_LENGTH = 12
# Argon2 no tiene el truncado de bcrypt, pero un tope evita que alguien mande un
# megabyte y nos cueste CPU derivarlo.
MAX_LENGTH = 128

_hasher = PasswordHasher()


class PasswordPolicyError(ValueError):
    """Mensaje pensado para enseñárselo a quien está eligiendo la contraseña."""


def validate(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(
            f"La contraseña necesita al menos {MIN_LENGTH} caracteres. La longitud es lo "
            f"que más cuesta de adivinar."
        )
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f"Como mucho {MAX_LENGTH} caracteres.")
    if not re.search(r"[a-z]", password):
        raise PasswordPolicyError("Falta alguna letra minúscula.")
    if not re.search(r"[A-Z]", password):
        raise PasswordPolicyError("Falta alguna letra mayúscula.")
    if not re.search(r"\d", password):
        raise PasswordPolicyError("Falta algún número.")


def hash_password(password: str) -> str:
    validate(password)
    return _hasher.hash(password)


def verify(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError):
        return False


def verify_dummy(password: str) -> None:
    """Verifica contra un hash señuelo, para que fallar cueste lo mismo que acertar.

    Sin esto, entrar con un usuario que no existe responde en un milisegundo y entrar
    con uno que sí existe tarda lo que tarda Argon2, unas decenas. El mensaje de error
    es el mismo, pero el cronómetro los distingue igual de bien, y el formulario vuelve
    a ser un buscador de cuentas: justo lo que el mensaje único quería evitar.

    Se llama en la rama de "usuario no encontrado" y no devuelve nada, porque su
    resultado no interesa: lo que interesa es el tiempo que gasta.
    """
    verify(password, _dummy_hash())


def _dummy_hash() -> str:
    """El hash señuelo, calculado la primera vez que hace falta.

    Perezoso y no al importar el módulo: derivar un Argon2 cuesta decenas de
    milisegundos y no hay razón para pagarlos en cada arranque, ni en cada test que
    toque este módulo sin llegar a iniciar sesión.
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _hasher.hash("senuelo-que-nunca-es-la-contrasena-de-nadie")
    return _DUMMY_HASH


_DUMMY_HASH: str | None = None


def needs_rehash(password_hash: str) -> bool:
    """True cuando el hash se creó con parámetros más flojos que los de ahora.

    Permite subir el coste de Argon2 con el tiempo sin obligar a nadie a cambiar de
    contraseña: se rehashea en el siguiente inicio de sesión correcto.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return False
