"""Router LLM local vs cloud.

`settings.LLM_PROVIDER` decide el backend ("cloud" = Claude, "local" = Ollama). Los
llamadores solo conocen `generate`/`stream` y las dos excepciones de aquí.

Reciben el historial completo (`messages`) y no un mensaje suelto: una historia sin
memoria de lo dicho no es una historia.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Literal, TypedDict

import anthropic
import httpx

from app.core.config import settings
from app.llm import cloud, local


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class LLMUnavailableError(Exception):
    """El proveedor no respondió: Ollama apagado, timeout o error de la API."""


class LLMRefusalError(LLMUnavailableError):
    """El modelo declinó la petición (`stop_reason == "refusal"`). Hereda de
    `LLMUnavailableError` para que quien no quiera distinguirlo no tenga que hacerlo."""


_PROVIDER_ERRORS = (httpx.HTTPError, anthropic.APIError)


def normalize_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Deja el historial en la forma que aceptan los dos proveedores.

    Fusiona turnos seguidos del mismo rol (queda uno de usuario huérfano si el LLM falló
    en el turno anterior) y descarta lo que no sea user/assistant: el rol del sistema
    solo entra por `system_prompt`, nunca desde datos guardados.
    """
    clean: list[ChatMessage] = []
    for m in messages:
        if m["role"] not in ("user", "assistant") or not m["content"].strip():
            continue
        if clean and clean[-1]["role"] == m["role"]:
            clean[-1] = {"role": m["role"], "content": f"{clean[-1]['content']}\n\n{m['content']}"}
        else:
            clean.append({"role": m["role"], "content": m["content"]})
    return clean


def demo_mode() -> bool:
    """Nube sin clave: el cliente responde un texto fijo. Quien necesite JSON válido
    tiene que saberlo antes de llamar, no descubrirlo al parsear."""
    return settings.LLM_PROVIDER != "local" and not settings.ANTHROPIC_API_KEY


def generate(system_prompt: str, messages: list[ChatMessage], *, max_tokens: int | None = None) -> str:
    msgs = normalize_messages(messages)
    try:
        if settings.LLM_PROVIDER == "local":
            return local.generate(system_prompt, msgs)
        return cloud.generate(system_prompt, msgs, max_tokens=max_tokens)
    except cloud.RefusalStop as exc:
        raise LLMRefusalError(str(exc)) from exc
    except _PROVIDER_ERRORS as exc:
        raise LLMUnavailableError(str(exc)) from exc


def stream(system_prompt: str, messages: list[ChatMessage]) -> Iterator[str]:
    msgs = normalize_messages(messages)
    try:
        if settings.LLM_PROVIDER == "local":
            yield from local.stream(system_prompt, msgs)
        else:
            yield from cloud.stream(system_prompt, msgs)
    except cloud.RefusalStop as exc:
        raise LLMRefusalError(str(exc)) from exc
    except _PROVIDER_ERRORS as exc:
        raise LLMUnavailableError(str(exc)) from exc
