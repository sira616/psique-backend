"""Cliente Ollama (local u Ollama Cloud), misma interfaz que `app.llm.cloud`.

Sirve para un Ollama local (`OLLAMA_BASE_URL=http://localhost:11434`, sin API key) y
para Ollama Cloud (`OLLAMA_BASE_URL=https://ollama.com`, sin `/api`, con `OLLAMA_API_KEY`): mismo `/api/chat`.
"""
from __future__ import annotations

import json
from collections.abc import Iterator

import httpx

from app.core.config import settings


def _messages(system_prompt: str, messages: list[dict]) -> list[dict]:
    return [{"role": "system", "content": system_prompt}, *messages]


def _headers() -> dict:
    if settings.OLLAMA_API_KEY:
        return {"Authorization": f"Bearer {settings.OLLAMA_API_KEY}"}
    return {}


def _body(system_prompt: str, messages: list[dict], stream: bool) -> dict:
    return {
        "model": settings.OLLAMA_MODEL,
        "messages": _messages(system_prompt, messages),
        "stream": stream,
        "keep_alive": settings.OLLAMA_KEEP_ALIVE,
    }


def generate(system_prompt: str, messages: list[dict]) -> str:
    response = httpx.post(
        f"{settings.OLLAMA_BASE_URL}/api/chat",
        json=_body(system_prompt, messages, False),
        headers=_headers(),
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["message"]["content"]


def stream(system_prompt: str, messages: list[dict]) -> Iterator[str]:
    with httpx.stream(
        "POST",
        f"{settings.OLLAMA_BASE_URL}/api/chat",
        json=_body(system_prompt, messages, True),
        headers=_headers(),
        timeout=120,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            content = chunk.get("message", {}).get("content", "")
            if content:
                yield content
            if chunk.get("done"):
                break
