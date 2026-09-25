import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, StringConstraints, Tag, field_validator, model_validator
from pydantic_core import PydanticCustomError

# Límites alineados con `CharacterProfile`: lo que pasa aquí tiene que caber allí. Donde el
# perfil añade texto derivado (el saludo envuelve el escenario en cursiva), el límite de
# entrada es algo menor que el del perfil.
MAX_PREMISE_LEN = 600
MAX_DESCRIPTION_LEN = 1000

NAME_PATTERN = r"^[A-Za-zÀ-ÿ' .-]+$"


def _text(min_length: int, max_length: int):
    return Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)
    ]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DefinedStoryIn(_Input):
    mode: Literal["definida"]
    title: _text(3, 80)
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=2, max_length=60, pattern=NAME_PATTERN),
    ]
    age: int
    # Texto libre; se trocea por comas, punto y coma o saltos de línea en rasgos.
    personality: _text(3, 300)
    speakingStyle: _text(10, 300)
    setting: _text(20, 590)
    tone: _text(3, 60)
    backstory: _text(40, 1200)
    hook: _text(10, 140) | None = None
    isPublic: bool = False
    freeFirstRead: bool = True
    adult: bool = False

    @field_validator("age")
    @classmethod
    def solo_adultos(cls, value: int) -> int:
        if not 18 <= value <= 90:
            raise ValueError("Los personajes tienen que ser adultos (entre 18 y 90 años)")
        return value


class ConceptStoryIn(_Input):
    mode: Literal["concepto"]
    premise: _text(20, MAX_PREMISE_LEN)
    tone: _text(3, 60) | None = None
    isPublic: bool = False
    freeFirstRead: bool = True
    adult: bool = False


def _mode(value: object) -> str | None:
    return value.get("mode") if isinstance(value, dict) else getattr(value, "mode", None)


# Con `Tag`, los errores de validación dicen `["body", "definida", "age"]` y no el nombre
# de la clase: el frontend los puede mapear a su formulario.
CustomStoryIn = Annotated[
    Annotated[DefinedStoryIn, Tag("definida")] | Annotated[ConceptStoryIn, Tag("concepto")],
    Discriminator(_mode),
]


class DefinitionOut(BaseModel):
    """Lo que escribió el usuario en el modo definida (rasgos ya normalizados)."""

    name: str
    age: int
    personality: str
    speakingStyle: str
    setting: str
    tone: str | None
    backstory: str


class CustomStoryOut(BaseModel):
    id: str
    characterId: str
    mode: str
    title: str
    hook: str
    # Texto largo del autor para la página del libro. Null si no ha escrito ninguno.
    description: str | None
    coverUrl: str | None
    premise: str | None
    tone: str | None
    # Solo en modo definida. El perfil de un concepto (mundo, secretos) nunca sale.
    definition: DefinitionOut | None
    isPublic: bool
    freeFirstRead: bool
    adult: bool
    publishedAt: datetime | None
    createdAt: datetime | None


# Lo que en el modo definida escribió el autor sobre el personaje. Solo se puede editar en
# ese modo: en concepto el perfil lo inventó el LLM (ver `custom_story_service.update_owned`).
CHARACTER_FIELDS = ("name", "age", "personality", "speakingStyle", "setting", "backstory")


def _no_nulo(value):
    if value is None:
        raise PydanticCustomError("required", "Este campo no se puede dejar vacío.")
    return value


def _acotado(value: str, min_length: int, max_length: int) -> str:
    """Recorta y comprueba los límites, con el mensaje ya en español.

    Los validadores lanzan `PydanticCustomError` y no `ValueError` por lo mismo que en
    `app.schemas.profile`: que `msg` llegue sin el prefijo "Value error, ".
    """
    value = value.strip()
    if len(value) < min_length:
        raise PydanticCustomError("too_short", f"Hacen falta al menos {min_length} caracteres.")
    if len(value) > max_length:
        raise PydanticCustomError("too_long", f"Como mucho {max_length} caracteres.")
    return value


class CustomStoryPatchIn(_Input):
    """Campo ausente = no cambia; hace falta al menos uno.

    `description` y `tone` a null o "" se borran; el resto no se puede vaciar. La premisa
    no está: es lo que se le dio al modelo para generar la historia y cambiarla después no
    cambiaría nada de lo generado.
    """

    title: str | None = None
    hook: str | None = None
    description: str | None = None
    tone: str | None = None
    isPublic: bool | None = None
    freeFirstRead: bool | None = None
    adult: bool | None = None
    # Solo en modo definida; mismos límites que `DefinedStoryIn`.
    name: str | None = None
    age: int | None = None
    personality: str | None = None
    speakingStyle: str | None = None
    setting: str | None = None
    backstory: str | None = None

    @field_validator(
        "title", "hook", "isPublic", "freeFirstRead", "adult",
        "name", "age", "personality", "speakingStyle", "setting", "backstory",
    )
    @classmethod
    def sin_nulos(cls, value):
        # Solo corre si el campo vino en el cuerpo: los valores por defecto no se validan.
        return _no_nulo(value)

    @field_validator("title")
    @classmethod
    def titulo(cls, value: str) -> str:
        return _acotado(value, 3, 80)

    @field_validator("hook")
    @classmethod
    def gancho(cls, value: str) -> str:
        return _acotado(value, 10, 140)

    @field_validator("description")
    @classmethod
    def descripcion(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if len(value) > MAX_DESCRIPTION_LEN:
            raise PydanticCustomError(
                "too_long", f"La descripción admite como mucho {MAX_DESCRIPTION_LEN} caracteres."
            )
        return value or None

    @field_validator("tone")
    @classmethod
    def tono(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        return _acotado(value, 3, 60) if value else None

    @field_validator("name")
    @classmethod
    def nombre(cls, value: str) -> str:
        value = _acotado(value, 2, 60)
        if not re.match(NAME_PATTERN, value):
            raise PydanticCustomError("name_format", "El nombre solo admite letras, espacios, apóstrofos y guiones.")
        return value

    @field_validator("age")
    @classmethod
    def edad(cls, value: int) -> int:
        if not 18 <= value <= 90:
            raise PydanticCustomError("age_range", "Los personajes tienen que ser adultos (entre 18 y 90 años)")
        return value

    @field_validator("personality")
    @classmethod
    def personalidad(cls, value: str) -> str:
        return _acotado(value, 3, 300)

    @field_validator("speakingStyle")
    @classmethod
    def forma_de_hablar(cls, value: str) -> str:
        return _acotado(value, 10, 300)

    @field_validator("setting")
    @classmethod
    def escenario(cls, value: str) -> str:
        return _acotado(value, 20, 590)

    @field_validator("backstory")
    @classmethod
    def trasfondo(cls, value: str) -> str:
        return _acotado(value, 40, 1200)

    @model_validator(mode="after")
    def al_menos_uno(self):
        # `model_fields_set` son los que vinieron en el cuerpo: así no hay que enumerarlos
        # aquí cada vez que el PATCH crece.
        if not self.model_fields_set:
            raise PydanticCustomError("missing", "Manda al menos un campo que cambiar.")
        return self
