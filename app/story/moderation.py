"""Moderación de lo que escribe el usuario: patrones y, encima, una segunda opinión del LLM.

Los patrones no entienden paráfrasis ni eufemismos ("quiero sentirte dentro de mí"). El
LLM sí, pero es lento, cuesta saldo y puede fallar, así que:

- Primero patrones. Si ya dan `prohibido` no se pregunta: más grave no puede ser.
- El LLM solo puede SUBIR el nivel (`stricter`). Un "ok" inyectado por el usuario no baja
  nada, y un falso negativo del modelo deja lo que dijeran los patrones.
- Si el LLM falla (timeout, error, JSON roto, nivel desconocido) mandan los patrones: no
  se bloquea a nadie ni se rompe el turno porque el proveedor vaya mal.
- Textos cortos sin nada que marquen los patrones no se preguntan, y las respuestas se
  guardan en una LRU por texto normalizado (sugerencias rápidas, reintentos).
- En modo demo no hay LLM real: solo patrones.

Se llama antes de abrir el stream y en serie con él: con Ollama Cloud (una petición
concurrente) la moderación nunca compite con la respuesta del personaje.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict

from app.core.config import settings
from app.llm import router as llm_router
from app.llm.prompts.moderation import MODERATION_SYSTEM_PROMPT, REASONS, build_moderation_input
from app.story import content_policy
from app.story.content_policy import Classification, ContentLevel

logger = logging.getLogger(__name__)

# El JSON pedido es de una línea; el margen cubre un modelo que se enrolla un poco.
MODERATION_MAX_TOKENS = 60

_JSON_OBJECT = re.compile(r"\{.*?\}", re.DOTALL)
_LETTERS = re.compile(r"[a-z]")


class ModerationError(Exception):
    """El LLM no dio un veredicto usable. Nunca sale de este módulo."""


def parse_verdict(raw: str) -> Classification:
    match = _JSON_OBJECT.search(raw or "")
    if match is None:
        raise ModerationError("sin JSON")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ModerationError("JSON mal formado") from exc
    if not isinstance(data, dict):
        raise ModerationError("JSON sin objeto")
    try:
        level = ContentLevel(content_policy.normalize(str(data.get("level", ""))).strip())
    except ValueError as exc:
        raise ModerationError("nivel desconocido") from exc
    reason = content_policy.normalize(str(data.get("reason", ""))).strip()
    # Solo motivos de la lista cerrada: el motivo va al incidente y no puede llevar texto
    # del usuario.
    return Classification(level, f"llm:{reason if reason in REASONS else 'otro'}")


class LlmInputClassifier:
    """`InputClassifier` con LLM y caché LRU. En fallo devuelve `ok`: combinado con
    `stricter`, eso deja el nivel de los patrones."""

    def __init__(self, cache_size: int | None = None):
        self._cache: OrderedDict[str, Classification] = OrderedDict()
        self._cache_size = cache_size if cache_size is not None else settings.LLM_MODERATION_CACHE_SIZE
        # Las rutas síncronas de FastAPI corren en un pool de hilos.
        self._lock = threading.Lock()

    def __call__(self, text: str) -> Classification:
        return self.classify(text) or Classification(ContentLevel.OK)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(content_policy.normalize(text).strip().encode("utf-8")).hexdigest()

    def classify(self, text: str) -> Classification | None:
        """Veredicto del LLM, o None si no lo hay. Los fallos no se guardan en caché: el
        siguiente intento vuelve a preguntar."""
        key = self._key(text)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        started = time.monotonic()
        try:
            raw = llm_router.generate(
                MODERATION_SYSTEM_PROMPT,
                [{"role": "user", "content": build_moderation_input(text)}],
                max_tokens=MODERATION_MAX_TOKENS,
                timeout=settings.LLM_MODERATION_TIMEOUT_SECONDS,
                json_output=True,
            )
            verdict = parse_verdict(raw)
        except Exception as exc:  # noqa: BLE001 - cualquier fallo deja mandar a los patrones
            logger.warning("Moderación con LLM sin veredicto (%s): %s", type(exc).__name__, exc)
            return None
        logger.debug("Moderación con LLM: %s en %.2fs", verdict.level.value, time.monotonic() - started)
        with self._lock:
            self._cache[key] = verdict
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return verdict


llm_classifier = LlmInputClassifier()


def enabled() -> bool:
    return settings.LLM_MODERATION_ENABLED and not llm_router.demo_mode()


def _worth_asking(text: str, patterns: Classification) -> bool:
    if patterns.level is ContentLevel.PROHIBIDO or not enabled():
        return False
    t = content_policy.normalize(text).strip()
    if not _LETTERS.search(t):
        return False
    # "hola", "vale", "jaja": nada que un LLM vaya a subir. Lo corto que los patrones ya
    # marcan como sensual sí se pregunta ("tu culo, ya").
    return len(t) >= settings.LLM_MODERATION_MIN_CHARS or patterns.level is not ContentLevel.OK


def classify_input(text: str) -> Classification:
    """Nivel de un mensaje de chat: el más grave entre patrones y LLM."""
    patterns = content_policy.classify_input(text)
    if not _worth_asking(text, patterns):
        return patterns
    return content_policy.stricter(patterns, llm_classifier(text))


def check_user_text(*texts: str | None) -> str | None:
    """`content_policy.check_user_text` más una sola consulta al LLM con todos los textos.

    Una consulta y no una por campo: una historia definida tiene ocho, y ocho llamadas en
    serie serían ocho esperas. Si el LLM lo sube a explícito o prohibido, el mismo 422 que
    daría un patrón.
    """
    rejection = content_policy.check_user_text(*texts)
    if rejection:
        return rejection
    joined = "\n\n".join(t.strip() for t in texts if t and t.strip())
    if not joined or not _worth_asking(joined, content_policy.classify_input(joined)):
        return None
    verdict = llm_classifier.classify(joined)
    if verdict is None or verdict.level in (ContentLevel.OK, ContentLevel.SENSUAL):
        return None
    if verdict.level is ContentLevel.PROHIBIDO:
        if verdict.rule == "llm:menores":
            return content_policy.MINORS_INPUT_MESSAGE
        return content_policy.PROHIBITED_INPUT_MESSAGE
    return content_policy.EXPLICIT_INPUT_MESSAGE
