"""Memoria del personaje: extractor LLM → JSON → Pydantic → lista blanca → base de datos.

Mismo patrón que el check-in de morfeo. El modelo propone; el código decide qué claves
existen (`FACT_KEYS`), qué forma tiene cada valor y qué se guarda. Una clave inventada
se ignora y un valor inválido se descarta solo, sin llevarse por delante los válidos.

La misma llamada devuelve las señales para la máquina de estados, también contra su
propia lista blanca (`state_machine.SIGNAL_WEIGHTS`): una llamada por turno y no dos.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import AfterValidator, StringConstraints, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.llm import router as llm_router
from app.llm.prompts.story import EXTRACTION_SYSTEM_PROMPT, build_extraction_input
from app.models.story import MemoryFact
from app.story.guardrail import contains_sensitive
from app.story.state_machine import MAX_SIGNALS_PER_TURN, SIGNAL_WEIGHTS

# Clave → cuántos valores se guardan como mucho. `nombre` es único: el nuevo sustituye.
FACT_KEYS: dict[str, int] = {
    "nombre": 1,
    "gustos": 6,
    "disgustos": 6,
    "aficiones": 6,
    "promesas": 4,
}
MAX_VALUES_PER_TURN = 3


def _no_sensitive(value: str) -> str:
    # Un email o un teléfono no es un "gusto": no entra en la memoria aunque el modelo lo
    # proponga, porque de ahí iría al prompt de cada turno siguiente.
    if contains_sensitive(value) or re.search(r"\d{5,}", value):
        raise ValueError("dato sensible")
    return value.strip()


NameValue = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=40, pattern=r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ' -]+$"),
]
FactText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=3, max_length=120),
    AfterValidator(_no_sensitive),
]

_NAME = TypeAdapter(NameValue)
_TEXT = TypeAdapter(FactText)
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class TurnExtraction:
    facts: dict[str, list[str]]
    signals: list[str]


EMPTY = TurnExtraction(facts={}, signals=[])


def validate_facts(raw: object) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, list[str]] = {}
    for key in FACT_KEYS:
        if key not in raw or raw[key] in (None, "", []):
            continue
        value = raw[key]
        if key == "nombre":
            try:
                clean[key] = [_NAME.validate_python(value)]
            except ValidationError:
                pass
            continue
        items = value if isinstance(value, list) else [value]
        valid = []
        for item in items[:MAX_VALUES_PER_TURN]:
            try:
                valid.append(_TEXT.validate_python(item))
            except ValidationError:
                continue
        if valid:
            clean[key] = list(dict.fromkeys(valid))
    return clean


def validate_signals(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen = [s for s in raw if isinstance(s, str) and s in SIGNAL_WEIGHTS]
    return list(dict.fromkeys(seen))[:MAX_SIGNALS_PER_TURN]


def parse_extraction(raw_text: str | None) -> TurnExtraction:
    match = _JSON_BLOCK.search(raw_text or "")
    if not match:
        return EMPTY
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return EMPTY
    if not isinstance(parsed, dict):
        return EMPTY
    return TurnExtraction(
        facts=validate_facts(parsed.get("hechos")),
        signals=validate_signals(parsed.get("senales")),
    )


def extract_turn(previous_reply: str, user_message: str) -> TurnExtraction:
    """Devuelve vacío si el modelo no coopera: un turno sin extraer no rompe la historia,
    solo deja de sumar."""
    try:
        raw = llm_router.generate(
            EXTRACTION_SYSTEM_PROMPT,
            [{"role": "user", "content": build_extraction_input(previous_reply, user_message)}],
            max_tokens=512,
        )
    except llm_router.LLMUnavailableError:
        return EMPTY
    return parse_extraction(raw)


def save_facts(db: DbSession, story_id: str, facts: dict[str, list[str]]) -> list[MemoryFact]:
    added = []
    for key, values in facts.items():
        if key not in FACT_KEYS:
            continue
        existing = list(
            db.scalars(
                select(MemoryFact)
                .where(MemoryFact.story_id == story_id, MemoryFact.key == key)
                .order_by(MemoryFact.id)
            )
        )
        known = {f.value.casefold() for f in existing}
        for value in values:
            if value.casefold() in known:
                continue
            fact = MemoryFact(story_id=story_id, key=key, value=value)
            db.add(fact)
            existing.append(fact)
            known.add(value.casefold())
            added.append(fact)
        # Tope por clave: se olvida lo más antiguo, como haría cualquiera.
        overflow = len(existing) - FACT_KEYS[key]
        for old in existing[: max(0, overflow)]:
            if old in added:
                added.remove(old)
                db.expunge(old)
            else:
                db.delete(old)
    db.flush()
    return added


def load_facts(db: DbSession, story_id: str) -> dict[str, list[str]]:
    facts: dict[str, list[str]] = {}
    for fact in db.scalars(
        select(MemoryFact).where(MemoryFact.story_id == story_id).order_by(MemoryFact.id)
    ):
        facts.setdefault(fact.key, []).append(fact.value)
    return facts
