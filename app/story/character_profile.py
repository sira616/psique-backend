"""Perfiles de personaje validados.

Mismo patrón que el perfil docente de platano: JSON versionado, `extra="forbid"` y
congelado. Un campo mal escrito en el JSON rompe al cargar, no a mitad de una escena.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

CHARACTERS_DIR = Path(__file__).with_name("characters")
_SLUG = r"^[a-z][a-z0-9-]{1,39}$"
_MAX_BYTES = 20_000

SecretText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=300)]


class SpeechStyle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    registro: str = Field(min_length=10, max_length=300)
    muletillas: tuple[str, ...] = Field(default=(), max_length=6)
    evita: tuple[str, ...] = Field(default=(), max_length=6)


class CharacterProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    id: str = Field(pattern=_SLUG)
    nombre: str = Field(min_length=2, max_length=60)
    edad: int
    tagline: str = Field(min_length=10, max_length=140)
    personalidad: tuple[str, ...] = Field(min_length=2, max_length=8)
    forma_de_hablar: SpeechStyle
    trasfondo: str = Field(min_length=40, max_length=1200)
    gustos: tuple[str, ...] = Field(default=(), max_length=8)
    # Lo que el personaje no hace pase lo que pase. Obligatorio: un personaje sin
    # límites declarados es uno cuyos límites decide el usuario.
    limites: tuple[str, ...] = Field(min_length=1, max_length=8)
    escenario_inicial: str = Field(min_length=20, max_length=600)
    saludo: str = Field(min_length=10, max_length=600)
    # Opcionales: los predefinidos no los usan; sí las historias propias. `mundo` y
    # `secretos` los inventa el LLM en el modo concepto y se revelan poco a poco.
    tono: str | None = Field(default=None, min_length=3, max_length=60)
    mundo: str | None = Field(default=None, min_length=20, max_length=800)
    secretos: tuple[SecretText, ...] = Field(default=(), max_length=5)
    # Solo cuenta en los predefinidos: en los propios manda `story_blueprints.free_first_read`.
    free_first_read: bool = True
    # +18: solo para cuentas que confirmaron ser mayores de edad. En los propios manda
    # `story_blueprints.adult`. No sube el techo de lo que se genera.
    adult: bool = False

    @field_validator("edad")
    @classmethod
    def solo_adultos(cls, value: int) -> int:
        # Historias románticas: ningún personaje puede ser menor, ni por error de datos.
        if not 18 <= value <= 90:
            raise ValueError("Los personajes tienen que ser adultos (18-90)")
        return value

    def render(self) -> str:
        habla = self.forma_de_hablar
        partes = [
            f"PERSONAJE: {self.nombre}, {self.edad} años. {self.tagline}",
            "Personalidad: " + "; ".join(self.personalidad) + ".",
            f"Forma de hablar: {habla.registro}",
        ]
        if habla.muletillas:
            partes.append("Expresiones propias (con moderación): " + ", ".join(habla.muletillas) + ".")
        if habla.evita:
            partes.append("Nunca dice: " + ", ".join(habla.evita) + ".")
        partes.append(f"Trasfondo: {self.trasfondo}")
        if self.gustos:
            partes.append("Le gusta: " + ", ".join(self.gustos) + ".")
        partes.append("Límites del personaje:\n" + "\n".join(f"- {l}" for l in self.limites))
        if self.tono:
            partes.append(f"Tono de la historia: {self.tono}.")
        if self.mundo:
            partes.append(f"Mundo: {self.mundo}")
        if self.secretos:
            partes.append(
                "Secretos y giros (solo los conoces tú):\n" + "\n".join(f"- {s}" for s in self.secretos)
            )
        partes.append(f"Escenario: {self.escenario_inicial}")
        return "\n".join(partes)


def parse_profile(raw: bytes, expected_id: str | None = None) -> CharacterProfile:
    if len(raw) > _MAX_BYTES:
        raise ValueError("Perfil de personaje demasiado grande")
    profile = CharacterProfile.model_validate_json(raw)
    if expected_id is not None and profile.id != expected_id:
        raise ValueError(f"El id {profile.id!r} no coincide con el fichero {expected_id!r}")
    return profile


@lru_cache(maxsize=1)
def load_characters() -> dict[str, CharacterProfile]:
    # Caché de proceso: los perfiles son parte del despliegue, no datos que cambien en caliente.
    profiles = {}
    for path in sorted(CHARACTERS_DIR.glob("*.json")):
        profile = parse_profile(path.read_bytes(), expected_id=path.stem)
        profiles[profile.id] = profile
    return profiles


def get_character(character_id: str) -> CharacterProfile | None:
    return load_characters().get(character_id)
