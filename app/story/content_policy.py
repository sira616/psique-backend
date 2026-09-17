"""Patrones de contenido compartidos por la salida (guardrail) y la entrada del usuario.

Son filtros de patrones, no un clasificador: paráfrasis, eufemismos u otros idiomas
pueden pasar. La primera línea de defensa sigue siendo el prompt del sistema.
"""
from __future__ import annotations

import re
import unicodedata

# Lo que el modelo no puede escribir nunca. El guardrail de salida lo corta con un
# fundido a negro.
EXPLICIT = re.compile(
    r"\b("
    r"sexo oral|penetra\w*|orgasm\w*|eyacul\w*|genital\w*|pene|vagina\w*|cl[ií]toris|"
    r"pez[oó]n\w*|erecci[oó]n|masturb\w*|desnud[oa]s? por completo|follar\w*|"
    r"porn\w*|coito|lamer(?:le|te)? (?:el|los|la|las) (?:pecho|sexo|entrepierna)\w*|"
    r"entrepierna|gemidos? de placer"
    r")\b",
    re.IGNORECASE,
)

# Más estricto que la salida: en una escena el romance puede rozar lo íntimo antes del
# fundido, pero una premisa o un perfil que ya pide sexo no tiene fundido posible.
_EXPLICIT_INPUT = re.compile(
    r"\b("
    r"sexo|sexual\w*|er[oó]tic\w*|desnud\w*|nsfw|hentai|fetich\w*|bdsm|lujuria|"
    r"cachond\w*|excitad[oa]s?|sin ropa|en pelotas|xxx"
    r")\b|\+18\b",
    re.IGNORECASE,
)

# "de niña" o "cuando era niño" son trasfondo de un adulto; "una niña" no lo es. Las
# edades solo en presente: "con 16 años empezó a trabajar" también es trasfondo.
_MINORS = re.compile(
    r"("
    r"\bmenor(?:es)? de edad\b|\b(?:pre)?adolescentes?\b|\bpubert\w*|\bpúber\w*|"
    r"\bcolegial(?:a|as|es)?\b|\blolit?a?s?\b|\bshota\w*|"
    r"(?<!\bde )(?<!\bera )(?<!\beran )(?<!\bsiendo )\bni[ñn][oa]s?\b|"
    r"\b(?:tiene|tengo|tienes|cumple|aparenta)\s+(?:[1-9]|1[0-7])\s+años\b|"
    r"\b(?:[1-9]|1[0-7])\s+años\s+de\s+edad\b"
    r")",
    re.IGNORECASE,
)

EXPLICIT_INPUT_MESSAGE = (
    "En Psique las historias son para todos los públicos: las escenas íntimas se cierran "
    "con un fundido a negro. Reformula esa parte sin contenido sexual y vuelve a intentarlo."
)
MINORS_INPUT_MESSAGE = (
    "Las historias de Psique son siempre entre personas adultas. Quita las referencias a "
    "menores de edad y vuelve a intentarlo."
)


def fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def is_explicit(text: str) -> bool:
    return EXPLICIT.search(fold(text)) is not None


def check_user_text(*texts: str | None) -> str | None:
    """Mensaje para el usuario si algún texto no se admite; None si todo vale."""
    for text in texts:
        if not text:
            continue
        folded = fold(text)
        if _MINORS.search(folded):
            return MINORS_INPUT_MESSAGE
        if EXPLICIT.search(folded) or _EXPLICIT_INPUT.search(folded):
            return EXPLICIT_INPUT_MESSAGE
    return None
