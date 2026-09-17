from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# El mensaje viaja al LLM dos veces (respuesta y extracción): su longitud es coste directo.
MAX_MESSAGE_LEN = 2000


class CharacterOut(BaseModel):
    """Personaje elegible para una historia.

    `origin="psique"`: predefinido, todos los campos rellenos. `origin="propia"`: creado
    por la cuenta; en modo concepto `name`, `age`, `tagline`, `traits` y `scenario` van a
    null para no destripar lo que el LLM inventó.
    """

    id: str
    origin: Literal["psique", "propia"] = "psique"
    mode: Literal["definida", "concepto"] | None = None
    title: str
    hook: str
    name: str | None
    age: int | None
    tagline: str | None
    traits: list[str] | None
    scenario: str | None


class QuickChoiceOut(BaseModel):
    id: str
    label: str


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    createdAt: datetime | None


class FactOut(BaseModel):
    key: str
    value: str


class StoryStateOut(BaseModel):
    phase: str
    phaseLabel: str
    phaseIndex: int
    phaseCount: int
    affinity: int
    turnCount: int
    quickChoices: list[QuickChoiceOut]
    # Capítulo = fase. Bloqueado: la fase siguiente ya se ganó y espera a pagarse.
    chapter_locked: bool = False
    next_phase: str | None = None
    chapter_cost: int = 0


class StoryOut(BaseModel):
    id: str
    characterId: str
    characterName: str
    state: StoryStateOut
    messages: list[MessageOut]
    facts: list[FactOut]
    createdAt: datetime | None
    status: Literal["activa", "archivada"]
    archivedAt: datetime | None


class StorySummaryOut(BaseModel):
    id: str
    characterId: str
    characterName: str
    state: StoryStateOut
    updatedAt: datetime | None
    status: Literal["activa", "archivada"]
    archivedAt: datetime | None


class StoryCreateIn(BaseModel):
    # Un predefinido ("lucia") o uno propio ("custom:<id>", ver `CUSTOM_PREFIX`).
    characterId: str = Field(min_length=1, max_length=40)


class ChatIn(BaseModel):
    """O texto libre o una de las sugerencias de la fase, nunca las dos: con las dos no
    habría forma de saber cuál de las dos cuenta como decisión."""

    message: str | None = Field(default=None, min_length=1, max_length=MAX_MESSAGE_LEN)
    choiceId: str | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.message is None) == (self.choiceId is None):
            raise ValueError("Manda `message` o `choiceId`, uno de los dos")
        if self.message is not None and not self.message.strip():
            raise ValueError("El mensaje está vacío")
        return self
