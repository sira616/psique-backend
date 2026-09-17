from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, StringConstraints, Tag, field_validator, model_validator
from pydantic_core import PydanticCustomError

# Límites alineados con `CharacterProfile`: lo que pasa aquí tiene que caber allí. Donde el
# perfil añade texto derivado (el saludo envuelve el escenario en cursiva), el límite de
# entrada es algo menor que el del perfil.
MAX_PREMISE_LEN = 600


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
        StringConstraints(
            strip_whitespace=True, min_length=2, max_length=60, pattern=r"^[A-Za-zÀ-ÿ' .-]+$"
        ),
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
    premise: str | None
    tone: str | None
    # Solo en modo definida. El perfil de un concepto (mundo, secretos) nunca sale.
    definition: DefinitionOut | None
    isPublic: bool
    freeFirstRead: bool
    adult: bool
    publishedAt: datetime | None
    createdAt: datetime | None


class CustomStoryPatchIn(_Input):
    """Campo ausente = no cambia; hace falta al menos uno."""

    isPublic: bool | None = None
    freeFirstRead: bool | None = None
    adult: bool | None = None

    @model_validator(mode="after")
    def al_menos_uno(self):
        if self.isPublic is None and self.freeFirstRead is None and self.adult is None:
            raise PydanticCustomError("missing", "Manda `isPublic`, `freeFirstRead` o `adult`.")
        return self
