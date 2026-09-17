"""Escena en curso y sugerencias para ella.

La misma llamada de extracción de cada turno propone un título de escena y tres
sugerencias. El modelo redacta; el código decide qué intenciones existen
(`state_machine.INTENT_WEIGHTS`), cuánto pesa cada una, qué ids tienen y qué se guarda.
Si la propuesta no pasa la validación entera, se usan las sugerencias fijas de la fase:
tres sugerencias a medias o repetidas son peores que tres genéricas.

Los ids llevan el turno en que se generaron (`t4s1`): así un botón de un turno anterior no
coincide nunca con uno de ahora y la ruta lo rechaza con 422.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError, field_validator

from app.story import state_machine as sm
from app.story.content_policy import check_user_text
from app.story.guardrail import contains_sensitive

MAX_SCENE_LEN = 60
MAX_LABEL_LEN = 40
MAX_MESSAGE_LEN = 220
SUGGESTION_COUNT = 3

ORIGIN_MODEL = "modelo"
ORIGIN_PHASE = "fase"

# Escena de reserva: la primera de una partida, o cuando nunca hubo una propuesta válida.
PHASE_SCENE_TITLES = {
    sm.Phase.CONOCERSE: "Primer encuentro",
    sm.Phase.CONFIANZA: "Ganando confianza",
    sm.Phase.TENSION: "Lo que no se dice",
    sm.Phase.CONFLICTO: "Un malentendido",
    sm.Phase.DESENLACE: "El final del camino",
}


def _safe(value: str) -> str:
    if check_user_text(value) is not None or contains_sensitive(value):
        raise ValueError("contenido no admitido")
    return value


SceneText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=MAX_SCENE_LEN)]
LabelText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=MAX_LABEL_LEN)]
MessageText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=MAX_MESSAGE_LEN)]
_SCENE = TypeAdapter(SceneText)


class _SuggestionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: str
    label: LabelText
    message: MessageText

    @field_validator("intent")
    @classmethod
    def known_intent(cls, value: str) -> str:
        if value not in sm.INTENT_WEIGHTS:
            raise ValueError("intención desconocida")
        return value

    @field_validator("label", "message")
    @classmethod
    def allowed(cls, value: str) -> str:
        return _safe(value)


class _SuggestionsIn(BaseModel):
    items: list[_SuggestionIn] = Field(min_length=SUGGESTION_COUNT, max_length=SUGGESTION_COUNT)

    @field_validator("items")
    @classmethod
    def distinct(cls, items: list[_SuggestionIn]) -> list[_SuggestionIn]:
        if len({i.intent for i in items}) != len(items):
            raise ValueError("intenciones repetidas")
        if len({i.label.casefold() for i in items}) != len(items):
            raise ValueError("etiquetas repetidas")
        if len({i.message.casefold() for i in items}) != len(items):
            raise ValueError("mensajes repetidos")
        return items


@dataclass(frozen=True)
class Suggestion:
    id: str
    intent: str
    label: str
    message: str

    @property
    def weight(self) -> int:
        return sm.INTENT_WEIGHTS[self.intent]


@dataclass(frozen=True)
class SceneSuggestions:
    scene: str
    items: tuple[Suggestion, ...]
    origin: Literal["modelo", "fase"]
    turn: int


# --- Validación de lo que propone el modelo ---------------------------------------------


def validate_scene(raw: object) -> str | None:
    try:
        return _safe(_SCENE.validate_python(raw))
    except (ValidationError, ValueError):
        return None


def validate_suggestions(raw: object) -> tuple[sm.QuickChoice, ...] | None:
    """Las tres sugerencias o nada."""
    try:
        parsed = _SuggestionsIn.model_validate({"items": raw})
    except ValidationError:
        return None
    return tuple(sm.QuickChoice(i.intent, i.label, i.message) for i in parsed.items)


# --- Construcción -----------------------------------------------------------------------


def _with_ids(choices: tuple[sm.QuickChoice, ...], turn: int) -> tuple[Suggestion, ...]:
    return tuple(
        Suggestion(id=f"t{turn}s{n}", intent=c.intent, label=c.label, message=c.message)
        for n, c in enumerate(choices, start=1)
    )


def phase_scene_title(phase: sm.Phase, character_name: str | None) -> str:
    base = PHASE_SCENE_TITLES[phase]
    title = f"{base} con {character_name}" if character_name else base
    return title if len(title) <= MAX_SCENE_LEN else base


def fallback(phase: sm.Phase, character_name: str | None, turn: int, scene: str | None = None) -> SceneSuggestions:
    """Sugerencias fijas de la fase. Conserva la escena anterior si la hay: que el modelo
    falle un turno no significa que haya cambiado de tema."""
    return SceneSuggestions(
        scene=scene or phase_scene_title(phase, character_name),
        items=_with_ids(sm.choices_for(phase), turn),
        origin=ORIGIN_PHASE,
        turn=turn,
    )


def resolve(
    phase: sm.Phase,
    character_name: str | None,
    turn: int,
    previous_scene: str | None,
    proposed_scene: str | None,
    proposed: tuple[sm.QuickChoice, ...] | None,
) -> SceneSuggestions:
    # Una escena válida sirve aunque las sugerencias no: el tema se ha leído bien igual.
    scene = proposed_scene or previous_scene
    if proposed is None:
        return fallback(phase, character_name, turn, scene)
    return SceneSuggestions(
        scene=scene or phase_scene_title(phase, character_name),
        items=_with_ids(proposed, turn),
        origin=ORIGIN_MODEL,
        turn=turn,
    )


# --- Persistencia -----------------------------------------------------------------------


def to_json(value: SceneSuggestions) -> dict:
    return {
        "origin": value.origin,
        "items": [{"id": s.id, "intent": s.intent, "label": s.label, "message": s.message} for s in value.items],
    }


def column_values(value: SceneSuggestions) -> dict:
    return {"scene_title": value.scene, "suggestions": to_json(value), "suggestions_turn": value.turn}


def load(story, character_name: str | None) -> SceneSuggestions:
    """Lo guardado en la partida. Las partidas anteriores a las escenas (o con datos que ya
    no valen, p. ej. una intención retirada) reciben la reserva de su fase."""
    phase = sm.Phase(story.phase)
    stored = story.suggestions if isinstance(story.suggestions, dict) else None
    if stored:
        try:
            items = tuple(
                Suggestion(id=str(i["id"]), intent=i["intent"], label=i["label"], message=i["message"])
                for i in stored.get("items", [])
                if i.get("intent") in sm.INTENT_WEIGHTS
            )
        except (AttributeError, KeyError, TypeError):
            items = ()
        if len(items) == SUGGESTION_COUNT:
            return SceneSuggestions(
                scene=story.scene_title or phase_scene_title(phase, character_name),
                items=items,
                origin=ORIGIN_MODEL if stored.get("origin") == ORIGIN_MODEL else ORIGIN_PHASE,
                turn=story.suggestions_turn or 0,
            )
    return fallback(phase, character_name, story.turn_count, story.scene_title)


def find(current: SceneSuggestions, choice_id: str) -> Suggestion | None:
    return next((s for s in current.items if s.id == choice_id), None)
