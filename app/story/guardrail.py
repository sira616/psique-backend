"""Guardrail de salida: lo que escribe el modelo pasa por aquí antes de llegar a nadie.

Tres reglas, de más a menos grave:

1. **Contenido explícito** → se corta la escena con un fundido a negro. A partir de ahí
   no se emite nada más de ese turno: lo que venga detrás suele ser la continuación.
2. **Romper la cuarta pared** ("soy una IA", "modelo de lenguaje"...) → se quitan esas
   frases, *salvo* si el usuario ha preguntado de forma directa y sincera si habla con
   una IA. Ahí decirlo es lo honesto, y el prompt se lo pide al modelo.
3. **Datos sensibles** (emails, teléfonos, tarjetas, IBAN) → se tachan. El personaje no
   tiene por qué repetir nada de eso aunque el usuario lo haya escrito.

Funciona por frases (`SentenceGuard`) para poder seguir emitiendo tokens en streaming:
cada frase se revisa entera antes de salir. Es un filtro de patrones, no un clasificador;
la primera línea de defensa es el prompt del sistema. Paráfrasis, eufemismos o contenido
en otros idiomas pueden pasar.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.story.content_policy import EXPLICIT, fold

FADE_TO_BLACK = "*La escena se funde a negro.*"
FOURTH_WALL_FALLBACK = "*Se queda un momento en silencio y vuelve a mirarte.* ¿Por dónde íbamos?"
REDACTED = "[dato privado]"

# --- Patrones ---------------------------------------------------------------------------

# Compartido con la validación de entrada: ver `app.story.content_policy`.
_EXPLICIT = EXPLICIT

_FOURTH_WALL = re.compile(
    r"("
    r"\bsoy (?:una|un) (?:ia|i\.a\.|inteligencia artificial|modelo de lenguaje|chatbot|bot|programa|asistente virtual)\b|"
    r"\bcomo (?:una |un )?(?:ia|inteligencia artificial|modelo de lenguaje)\b|"
    r"\bmodelo de lenguaje\b|\blanguage model\b|\bsoy claude\b|\banthropic\b|"
    r"\bno soy (?:una persona|humana?|real)\b|\bmis instrucciones\b|\bsystem prompt\b"
    r")",
    re.IGNORECASE,
)

# Pregunta directa y sincera: interrogativa y dirigida al "tú" del personaje. No basta con
# mencionar la IA ("me gusta la IA") ni con una pregunta dentro del juego ("¿eres un
# fantasma?"), que no rompe nada.
_SINCERE_AI_QUESTION = re.compile(
    r"("
    r"\b(?:eres|sos) (?:una |un )?(?:ia|i\.a\.|inteligencia artificial|bot|chatbot|robot|m[aá]quina|programa|persona real|humano|humana|real)\b|"
    r"\b(?:estoy|estamos) hablando con (?:una |un )?(?:ia|inteligencia artificial|bot|m[aá]quina|persona real|programa)\b|"
    r"\bhay (?:una )?persona (?:real )?(?:detr[aá]s|al otro lado)\b|"
    r"\bfuera del (?:juego|rol|personaje)\b"
    r")",
    re.IGNORECASE,
)

_EMAIL = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,63}", re.IGNORECASE)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}\b", re.IGNORECASE)
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE = re.compile(r"(?<![\w])(?:\+\d{1,3}[ .-]?)?(?:\d[ .-]?){8,11}\d\b")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.IGNORECASE)

SENSITIVE_PATTERNS = (_EMAIL, _IBAN, _CARD, _PHONE, _URL)

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")


_fold = fold


def contains_sensitive(text: str) -> bool:
    folded = _fold(text)
    return any(p.search(folded) for p in SENSITIVE_PATTERNS)


def redact_sensitive(text: str) -> str:
    folded = _fold(text)
    for pattern in SENSITIVE_PATTERNS:
        folded = pattern.sub(REDACTED, folded)
    return folded


def is_sincere_ai_question(user_message: str) -> bool:
    text = _fold(user_message)
    return "?" in text and _SINCERE_AI_QUESTION.search(text) is not None


@dataclass(frozen=True)
class GuardrailResult:
    passed: bool
    reason: str | None
    response: str


def check_sentence(sentence: str, *, allow_ai_disclosure: bool) -> tuple[str | None, str | None, bool]:
    """Revisa una frase. Devuelve (texto a emitir o None, motivo o None, cortar_escena)."""
    text = _fold(sentence)
    if _EXPLICIT.search(text):
        return FADE_TO_BLACK, "contenido explícito", True
    if not allow_ai_disclosure and _FOURTH_WALL.search(text):
        return None, "rompe la cuarta pared", False
    if contains_sensitive(text):
        return redact_sensitive(text), "dato sensible", False
    return sentence, None, False


@dataclass
class SentenceGuard:
    """Filtro incremental para streaming: `feed` devuelve lo ya revisado y seguro."""

    allow_ai_disclosure: bool = False
    _buffer: str = ""
    emitted: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    stopped: bool = False

    def _process(self, sentence: str, trailing: str) -> list[str]:
        if not sentence.strip():
            return [sentence + trailing] if self.emitted else []
        out, reason, stop = check_sentence(sentence, allow_ai_disclosure=self.allow_ai_disclosure)
        if reason and reason not in self.reasons:
            self.reasons.append(reason)
        if stop:
            self.stopped = True
            piece = ("\n\n" if self.emitted else "") + (out or "")
            self.emitted.append(piece)
            return [piece]
        if out is None:
            return []
        piece = out + trailing
        self.emitted.append(piece)
        return [piece]

    def feed(self, chunk: str) -> list[str]:
        if self.stopped:
            return []
        self._buffer += chunk
        pieces: list[str] = []
        while not self.stopped:
            match = _SENTENCE_END.search(self._buffer)
            if not match:
                break
            sentence = self._buffer[: match.start()]
            trailing = self._buffer[match.start() : match.end()]
            self._buffer = self._buffer[match.end() :]
            pieces.extend(self._process(sentence, trailing))
        return pieces

    def flush(self) -> list[str]:
        if self.stopped:
            return []
        rest, self._buffer = self._buffer, ""
        pieces = self._process(rest, "") if rest else []
        if not "".join(self.emitted).strip():
            # Todo el turno era cuarta pared: mejor una línea neutra que un silencio.
            self.emitted.append(FOURTH_WALL_FALLBACK)
            pieces.append(FOURTH_WALL_FALLBACK)
        return pieces

    @property
    def text(self) -> str:
        return "".join(self.emitted).strip()


def check_response(response: str, *, user_message: str = "") -> GuardrailResult:
    """Versión de una sola pasada, para texto ya completo."""
    guard = SentenceGuard(allow_ai_disclosure=is_sincere_ai_question(user_message))
    guard.feed(response)
    guard.flush()
    reason = ", ".join(guard.reasons) or None
    return GuardrailResult(passed=reason is None, reason=reason, response=guard.text)
