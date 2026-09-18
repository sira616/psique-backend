from __future__ import annotations

from collections.abc import Iterator

from anthropic import Anthropic

from app.core.config import settings

_client: Anthropic | None = None

DEMO_TEXT = (
    "[modo demo sin ANTHROPIC_API_KEY] *Te mira con una sonrisa a medias.* "
    "Aquí respondería el personaje siguiendo la fase de la historia. "
    "Configura ANTHROPIC_API_KEY en el .env para oír su voz de verdad."
)


class RefusalStop(Exception):
    """El modelo cortó con `stop_reason == "refusal"`. El router lo traduce."""


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def generate(
    system_prompt: str, messages: list[dict], *, max_tokens: int | None = None, timeout: float | None = None
) -> str:
    if not settings.ANTHROPIC_API_KEY:
        return DEMO_TEXT
    client = _get_client()
    if timeout is not None:
        # Sin reintentos: quien pone un plazo corto prefiere fallar a esperar el doble.
        client = client.with_options(timeout=timeout, max_retries=0)
    response = client.messages.create(
        model=settings.LLM_CLOUD_MODEL,
        max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
        system=system_prompt,
        messages=messages,
    )
    if response.stop_reason == "refusal":
        raise RefusalStop("El modelo declinó la petición")
    return "".join(block.text for block in response.content if block.type == "text")


def stream(system_prompt: str, messages: list[dict]) -> Iterator[str]:
    if not settings.ANTHROPIC_API_KEY:
        size = max(1, len(DEMO_TEXT) // 6)
        for i in range(0, len(DEMO_TEXT), size):
            yield DEMO_TEXT[i : i + size]
        return

    with _get_client().messages.stream(
        model=settings.LLM_CLOUD_MODEL,
        max_tokens=settings.LLM_MAX_TOKENS,
        system=system_prompt,
        messages=messages,
    ) as stream_ctx:
        yield from stream_ctx.text_stream
        # Una negativa puede llegar a mitad: lo ya emitido pasó el guardrail, pero el
        # turno no debe darse por bueno.
        if stream_ctx.get_final_message().stop_reason == "refusal":
            raise RefusalStop("El modelo declinó la petición")
