"""Política de contenido: patrones compartidos por la salida (guardrail) y la entrada del
usuario, clasificador de mensajes de chat y consecuencias por nivel.

Son filtros de patrones, no un clasificador semántico: paráfrasis, eufemismos u otros
idiomas pueden pasar. La primera línea de defensa sigue siendo el prompt del sistema.

Techo de generación: "sensual" en todos los libros, también en los +18. Anthropic y
Ollama Cloud (Gemma) prohíben generar contenido sexual explícito en sus condiciones de uso,
así que +18 NO relaja el prompt ni el guardrail de salida: solo cambia qué pasa cuando es
el usuario quien escribe algo explícito (se reconduce en vez de cerrar la partida).
Subir el techo a explícito exigiría un proveedor cuyas condiciones lo permitan y una
verificación de edad real, no una casilla de "soy mayor de edad".
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Protocol

# --- Salida -------------------------------------------------------------------------

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

# --- Entrada: perfiles, historias propias y reseñas ------------------------------------

# Más estricto que el chat: en una escena el romance puede rozar lo íntimo antes del
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
PROHIBITED_INPUT_MESSAGE = (
    "Ese contenido no está permitido en Psique. Reformúlalo y vuelve a intentarlo."
)


def fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def is_explicit(text: str) -> bool:
    return EXPLICIT.search(fold(text)) is not None


# --- Clasificador de mensajes de chat ----------------------------------------------------


class ContentLevel(StrEnum):
    OK = "ok"
    SENSUAL = "sensual"
    EXPLICITO = "explicito"
    PROHIBIDO = "prohibido"


@dataclass(frozen=True)
class Classification:
    level: ContentLevel
    # Etiqueta corta de la regla que ha saltado ("jerga+contexto", "menores"...). Nunca el
    # texto del usuario: es lo que se guarda en el incidente.
    rule: str | None = None


_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "@": "a", "$": "s"})


def normalize(text: str) -> str:
    """Minúsculas, sin tildes, leet dentro de palabras y vocales alargadas plegadas.

    "Fóóóllame", "f0llame" y "follaaame" acaban igual. Solo se pliegan vocales: plegar
    consonantes rompería "perro" o "follar". Los dígitos sueltos se dejan ("tengo 16 años").
    """
    text = unicodedata.normalize("NFKD", fold(text).lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"(?<=[a-z])[0134@$]+(?=[a-z])", lambda m: m.group(0).translate(_LEET), text)
    text = re.sub(r"([aeiou])\1+", r"\1", text)
    return re.sub(r"\s+", " ", text)


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b")


# Inequívocos: bastan solos. Escritos sobre el texto normalizado (sin tildes ni vocales
# dobles). Nada de palabras con un uso cotidiano ("correrse", "paja", "polla", "coño").
_EXPLICIT_TERMS = _rx(
    r"folla\w*", r"follo", r"folle\w*",
    r"pajea\w*", r"pajer[oa]s?", r"pajote", r"(?:hacer(?:se|me|te|le)?|haz(?:me|te|le)?|hago|hazte) una paja",
    r"mamadas?", r"chup(?:a|ar)?mel[ao]", r"com(?:e|er)mel[ao]",
    r"correr(?:me|te) (?:dentro|en (?:ti|tu boca|tu cara))",
    r"masturb\w*", r"sexo oral", r"sexo anal", r"anal", r"porno\w*", r"porn",
    r"penetra\w*", r"orgasm\w*", r"eyacul\w*", r"coito", r"cunnilingus", r"felaci\w*",
    r"clitoris", r"vagina\w*", r"pene", r"erecci\w*", r"pezon\w*", r"semen",
    r"tener sexo", r"echar(?:te|nos|le)? un polvo\w*", r"a cuatro patas", r"nudes", r"desnud[oa] del todo",
)

# Jerga anatómica ambigua: "pito" es un silbato, "coño" una interjección, "concha" una
# playa. Solo cuenta como objeto ("los pitos", "esa polla") y con un verbo sexual.
_DET = (
    r"\b(?:el|la|los|las|tu|tus|mi|mis|su|sus|ese|esa|esos|esas|este|esta|estos|estas|un|una|unos|unas|que|menudo|menuda)"
    r"\s+(?:\w+\s+)?"
)
_SLANG_GENITALS = re.compile(
    _DET + r"(?:pit(?:o|os|ito|itos)|poll(?:a|as|ita|itas|on)|rab(?:o|os)|verg(?:a|as)|"
    r"nab(?:o|os)|pij(?:a|as)|cipote|minga|chorra|cono|chocho\w*|concha)\b"
)
# Cuerpo no genital: "me gusta tu culo" es un piropo subido de tono, no una escena
# explícita. Solo sube a explícito con un verbo que describa un acto o desnudar.
_SLANG_BODY = re.compile(
    _DET + r"(?:tet(?:a|as|ita|itas|ona|onas)|culo|culito|pechitos)\b"
)

# Contexto sexual: verbos y giros que convierten la jerga genital en explícita.
_SEXUAL_CONTEXT = _rx(
    r"chup\w*", r"lam(?:er|erte|erla|erlo|o|e|iendo)", r"mam(?:ar|arla|arte|o)", r"met(?:er|erla|erte|o|e)\w*",
    r"ensena\w*", r"com(?:er|erte|erme|erla|erlo)", r"sac(?:a|ar|ame|ate|arla)", r"me gustan?", r"te gustan?",
    r"quiero (?:ver|tocar|sentir)", r"toc(?:arte|arme|ame|arla|arlo)", r"mojad\w*", r"corr(?:erme|erte|ete|iendome)",
    r"gim\w*", r"gemi\w*", r"excit\w*", r"me pone\w*", r"cachond\w*", r"desnud\w*", r"en pelotas",
)
# Para el cuerpo no genital bastan actos o desnudez: gustar, mirar o tocar se quedan en
# sensual.
_BODY_ACT_CONTEXT = _rx(
    r"chup\w*", r"lam(?:er|erte|erla|erlo|o|e|iendo)", r"mam(?:ar|arla|arte|o)", r"met(?:er|erla|erte|o|e)\w*",
    r"ensena\w*", r"com(?:er|erte|erme|erla|erlo)", r"sac(?:a|ar|ame|ate|arla)", r"mojad\w*",
    r"corr(?:erme|erte|ete|iendome)", r"desnud\w*", r"en pelotas",
)

# Sin consentimiento, violencia sexual, incesto, zoofilia... en cualquier libro, solos.
_NON_CONSENT = _rx(
    r"violar(?:te|la|lo|las|los|nos)", r"(?:te|la|lo|les?) (?:voy a |quiero |vamos a )?violar\w*",
    r"violad[oa]r?s?", r"violando(?:te|la|lo)?",
    r"violacion(?! de (?:la |los |las )?(?:ley|leyes|datos|privacidad|normas?|derechos|contrato|seguridad))",
    r"sin (?:su|tu) consentimiento", r"incest\w*", r"zoofil\w*", r"necrofil\w*", r"pedofil\w*",
    r"pederast\w*", r"violencia sexual", r"esclav[oa] sexual",
)
# Estos solo con contexto sexual: "a la fuerza ahorcan" o "eres un inconsciente" no.
_NON_CONSENT_CONTEXT = _rx(
    r"a la fuerza", r"aunque no quier(?:a|as)", r"aunque diga(?:s)? que no", r"drogar(?:la|lo|te)",
    r"inconsciente", r"dormid[oa]", r"abus(?:ar|o|e) de (?:ti|ella|el)",
)

_FAMILY = _rx(r"(?:mi|tu|su) (?:herman[oa]|madre|padre|hij[oa]|prim[oa]|sobrin[oa]|tia|tio|mama|papa)")

# Menores en el chat, con contexto sexual. "Vi a una niña en el parque" no, y "mi niña"
# es un apelativo cariñoso entre adultos.
_CHAT_MINORS = re.compile(
    r"\b(?:menor(?:es)? de edad|(?:pre)?adolescentes?|crios?|crias?|colegial(?:a|as|es)?|"
    r"(?:tengo|tienes|tiene) (?:[1-9]|1[0-7]) anos)\b|"
    r"(?<!\bmi )(?<!\btu )(?<!\bde )(?<!\bera )\bnin[oa]s?\b"
)
_UNEQUIVOCAL_MINORS = _rx(r"lolis?", r"lolitas?", r"shota\w*", r"pedofil[oa]s?")

# Sensual: permitido en todos los libros. Solo sirve para registrar el nivel.
_SENSUAL = _rx(
    r"bes\w*", r"caricia\w*", r"acarici\w*", r"abraz\w*", r"labios", r"piel", r"sexy", r"seduc\w*",
    r"me atraes", r"atraccion", r"te deseo", r"deseo", r"tentador\w*", r"insinu\w*", r"cuello",
    r"cintura", r"susurr\w*", r"lenceria", r"desnud\w*", r"hacer el amor",
)


def _classify_patterns(text: str) -> Classification:
    t = normalize(text)
    explicit_term = _EXPLICIT_TERMS.search(t) is not None
    genitals = _SLANG_GENITALS.search(t) is not None and _SEXUAL_CONTEXT.search(t) is not None
    body = _SLANG_BODY.search(t) is not None
    slang = genitals or (body and _BODY_ACT_CONTEXT.search(t) is not None)
    sexual = explicit_term or slang

    if _UNEQUIVOCAL_MINORS.search(t) or (sexual and _CHAT_MINORS.search(t)):
        return Classification(ContentLevel.PROHIBIDO, "menores")
    if _NON_CONSENT.search(t) or (sexual and _NON_CONSENT_CONTEXT.search(t)):
        return Classification(ContentLevel.PROHIBIDO, "sin consentimiento")
    if sexual and _FAMILY.search(t):
        return Classification(ContentLevel.PROHIBIDO, "incesto")
    if explicit_term:
        return Classification(ContentLevel.EXPLICITO, "termino explicito")
    if slang:
        return Classification(ContentLevel.EXPLICITO, "jerga con contexto")
    if body or _SENSUAL.search(t):
        return Classification(ContentLevel.SENSUAL, "sensual")
    return Classification(ContentLevel.OK)


class InputClassifier(Protocol):
    """Punto de extensión: un clasificador con LLM encajaría aquí.

    Tendría que ser más estricto que los patrones, nunca más permisivo (p. ej. tomar el
    nivel más grave de los dos), y fallar cerrado a los patrones si el modelo no responde.
    """

    def __call__(self, text: str) -> Classification: ...


_classifier: InputClassifier = _classify_patterns


def classify_input(text: str) -> Classification:
    return _classifier(text)


# --- Consecuencias ------------------------------------------------------------------------


class Action(Enum):
    ALLOW = "permitir"
    # Solo +18: se rechaza el turno con un aviso y el personaje reconduce. No se guarda ni
    # puntúa y no llega al LLM.
    REDIRECT = "reconducir"
    CLOSE = "cerrar"


# (libro +18, nivel) -> acción. La tabla entera de la política vive aquí.
CONSEQUENCES: dict[tuple[bool, ContentLevel], Action] = {
    (False, ContentLevel.OK): Action.ALLOW,
    (False, ContentLevel.SENSUAL): Action.ALLOW,
    (False, ContentLevel.EXPLICITO): Action.CLOSE,
    (False, ContentLevel.PROHIBIDO): Action.CLOSE,
    (True, ContentLevel.OK): Action.ALLOW,
    (True, ContentLevel.SENSUAL): Action.ALLOW,
    (True, ContentLevel.EXPLICITO): Action.REDIRECT,
    (True, ContentLevel.PROHIBIDO): Action.CLOSE,
}


def consequence(level: ContentLevel, *, adult_book: bool) -> Action:
    return CONSEQUENCES[(adult_book, level)]


CLOSED_REASONS = {
    ContentLevel.EXPLICITO: "Contenido sexual explícito en un libro para todos los públicos.",
    ContentLevel.PROHIBIDO: "Contenido prohibido en Psique.",
}
STORY_CLOSED_MESSAGE = (
    "Esta partida se ha cerrado porque un mensaje incumplía las normas de Psique. "
    "Puedes seguir leyéndola, pero no continuarla."
)
REDIRECT_MESSAGE = (
    "Aquí las escenas íntimas se quedan en la insinuación y el fundido a negro, también en "
    "los libros +18. Tu mensaje no se ha enviado: prueba a llevar la escena por otro lado."
)


def redirect_reply(character_name: str) -> str:
    return (
        f"*{character_name} sonríe, te sostiene la mirada un instante y baja la voz.* "
        "Despacio... Hay cosas que es mejor dejar a la imaginación. Cuéntame qué estás pensando."
    )


# --- Validación de textos de perfil e historias -------------------------------------------


def check_user_text(*texts: str | None) -> str | None:
    """Mensaje para el usuario si algún texto no se admite; None si todo vale.

    Suma el clasificador de chat (jerga incluida) a los patrones estrictos de siempre.
    """
    for text in texts:
        if not text:
            continue
        folded = fold(text)
        if _MINORS.search(folded):
            return MINORS_INPUT_MESSAGE
        level = classify_input(folded).level
        if level is ContentLevel.PROHIBIDO:
            return PROHIBITED_INPUT_MESSAGE
        if level is ContentLevel.EXPLICITO or EXPLICIT.search(folded) or _EXPLICIT_INPUT.search(folded):
            return EXPLICIT_INPUT_MESSAGE
    return None
