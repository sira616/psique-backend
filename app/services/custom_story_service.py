"""Historias propias: un personaje y su mundo creados por la cuenta.

Dos modos:
- **definida**: el usuario escribe el personaje; el código lo traduce a `CharacterProfile`
  rellenando lo que falta (límites, saludo, gancho). No interviene el LLM.
- **concepto**: el usuario da una premisa; el LLM inventa el perfil en JSON, que se valida
  contra el mismo esquema. Un único reintento si no valida.

En los dos, los límites del personaje los pone el código: son lo único que no se deja
decidir ni al usuario ni al modelo.

Visibilidad: una historia es privada hasta que su dueño la publica. Pública, sale en
Explorar y cualquier cuenta puede empezar una partida con ella. Las partidas ya empezadas
no dependen de que siga pública ni de que siga existiendo (ver `delete_owned`).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from app.llm import router as llm_router
from app.llm.prompts.custom_story import CONCEPT_SYSTEM_PROMPT, build_concept_input, build_retry_input
from app.models.story import MemoryFact, Message, Story, StoryBlueprint, StoryEvent
from app.models.user import User
from app.schemas.custom_story import ConceptStoryIn, CustomStoryOut, DefinedStoryIn, DefinitionOut
from app.schemas.profile import AuthorOut, StoryCardOut
from app.schemas.story import CharacterOut
from app.services import media_service
from app.story.character_profile import CharacterProfile
from app.story import moderation
from app.story.content_policy import check_user_text

CUSTOM_PREFIX = "custom:"
MAX_BLUEPRINTS_PER_USER = 50
HOOK_MAX = 140

DEFAULT_LIMITS = (
    "No participa en contenido sexual explícito: si la escena va hacia ahí, cierra con un fundido a negro y retoma después.",
    "No tolera faltas de respeto: si alguien le humilla, se distancia y lo dice.",
    "No pide ni guarda datos personales reales (teléfono, dirección, email).",
    "No finge que la historia es real si le preguntan en serio.",
)
# Si el usuario da un único rasgo, el esquema pide dos: este es neutro y no contradice nada.
FILLER_TRAIT = "se abre poco a poco con quien le trata con respeto"

# Lo que el LLM puede proponer del perfil. Lo que no esté aquí se ignora, no se valida.
_LLM_PROFILE_KEYS = (
    "nombre", "edad", "tagline", "personalidad", "forma_de_hablar", "trasfondo", "gustos",
    "mundo", "secretos", "escenario_inicial", "saludo",
)
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

GENERATION_FAILED_MESSAGE = (
    "No se ha podido crear una historia a partir de esa idea: el modelo devolvió un "
    "perfil no válido dos veces. Prueba a reformular la premisa."
)
LIMIT_MESSAGE = (
    f"Has llegado al máximo de {MAX_BLUEPRINTS_PER_USER} historias propias. Borra alguna "
    "para crear otra."
)


class ContentRejectedError(Exception):
    """El texto del usuario no se admite. El mensaje es apto para mostrarse."""


class BlueprintLimitError(Exception):
    pass


class ConceptGenerationError(Exception):
    pass


def character_ref(blueprint_id: str) -> str:
    return f"{CUSTOM_PREFIX}{blueprint_id}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def derive_hook(setting: str) -> str:
    first = _SENTENCE_END.split(setting.strip(), maxsplit=1)[0]
    return _clip(first if len(first) >= 10 else setting, HOOK_MAX)


# --- Modo definida ----------------------------------------------------------------------


def build_defined_profile(payload: DefinedStoryIn, blueprint_id: str) -> tuple[CharacterProfile, str]:
    traits = [t.strip() for t in re.split(r"[,;\n]+", payload.personality) if len(t.strip()) >= 2][:8]
    if len(traits) < 2:
        traits.append(FILLER_TRAIT)
    hook = payload.hook or derive_hook(payload.setting)
    profile = CharacterProfile(
        schema_version=1,
        id=f"propia-{blueprint_id}",
        nombre=payload.name,
        edad=payload.age,
        tagline=hook,
        personalidad=tuple(traits),
        forma_de_hablar={"registro": payload.speakingStyle},
        trasfondo=payload.backstory,
        limites=DEFAULT_LIMITS,
        escenario_inicial=payload.setting,
        # La historia arranca narrando el escenario: sin inventar una voz que el usuario no dio.
        saludo="*" + payload.setting.replace("*", "") + "*",
        tono=payload.tone,
    )
    return profile, hook


# --- Modo concepto ----------------------------------------------------------------------

_Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=80)]
_Hook = Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=HOOK_MAX)]


class _ConceptEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    titulo: _Title
    gancho: _Hook
    perfil: dict


def parse_concept(raw: str | None, blueprint_id: str, tone: str | None) -> tuple[str, str, CharacterProfile]:
    """Valida la respuesta del modelo. Lanza ValueError con un motivo breve si no vale."""
    match = _JSON_BLOCK.search(raw or "")
    if not match:
        raise ValueError("no contiene un objeto JSON")
    try:
        data = json.loads(match.group(0))
        envelope = _ConceptEnvelope.model_validate(data)
        proposed = {k: envelope.perfil[k] for k in _LLM_PROFILE_KEYS if k in envelope.perfil}
        profile = CharacterProfile.model_validate(
            proposed
            | {
                "schema_version": 1,
                "id": f"propia-{blueprint_id}",
                "limites": DEFAULT_LIMITS,
                "tono": tone,
            }
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON mal formado ({exc.msg})") from exc
    except ValidationError as exc:
        errores = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:6]
        )
        raise ValueError(errores) from exc
    if not profile.mundo or not profile.secretos:
        raise ValueError("faltan `mundo` o `secretos`")
    # Lo generado pasa el mismo filtro que la entrada: un perfil explícito no se guarda.
    if check_user_text(envelope.titulo, envelope.gancho, json.dumps(proposed, ensure_ascii=False)):
        raise ValueError("contenido no permitido (sexual o con menores)")
    return envelope.titulo, envelope.gancho, profile


_DEMO_NAMES = ("Irene Salas", "Julián Ortega", "Noa Beltrán", "Darío Campos", "Vera Montes", "Álex Rivas")


def demo_concept(premise: str, tone: str | None, blueprint_id: str) -> tuple[str, str, CharacterProfile]:
    """Sin clave de API: un perfil fijo derivado de la premisa, sin red y reproducible."""
    seed = int(hashlib.sha256(premise.encode("utf-8")).hexdigest(), 16)
    nombre = _DEMO_NAMES[seed % len(_DEMO_NAMES)]
    resumen = _clip(premise, 120)
    profile = CharacterProfile(
        schema_version=1,
        id=f"propia-{blueprint_id}",
        nombre=nombre,
        edad=25 + seed % 20,
        tagline=_clip(f"[demo] {resumen}", HOOK_MAX),
        personalidad=("reservada al principio", "observadora", "con un humor que tarda en asomar"),
        forma_de_hablar={"registro": "Frases cortas y directas; se suelta a medida que coge confianza."},
        trasfondo=f"[modo demo sin ANTHROPIC_API_KEY] Personaje de prueba nacido de esta idea: {premise}",
        limites=DEFAULT_LIMITS,
        mundo=_clip(f"El mundo de la premisa, tal cual la escribió el usuario: {premise}", 800),
        secretos=("Esconde algo sobre su pasado que solo contará cuando confíe en ti.",),
        escenario_inicial=_clip(f"La historia empieza aquí: {premise}", 600),
        saludo=f"*{nombre.split()[0]} te mira un momento antes de hablar.* Vaya, no esperaba a nadie a estas horas.",
        tono=tone,
    )
    return _clip(premise, 60), profile.tagline, profile


def generate_concept(premise: str, tone: str | None, blueprint_id: str) -> tuple[str, str, CharacterProfile]:
    if llm_router.demo_mode():
        return demo_concept(premise, tone, blueprint_id)
    messages = [{"role": "user", "content": build_concept_input(premise, tone)}]
    last_error = ""
    for attempt in range(2):
        raw = llm_router.generate(CONCEPT_SYSTEM_PROMPT, messages, max_tokens=2048)
        try:
            return parse_concept(raw, blueprint_id, tone)
        except ValueError as exc:
            last_error = str(exc)
        if attempt == 0:
            messages = messages + [
                {"role": "assistant", "content": (raw or "")[:4000]},
                {"role": "user", "content": build_retry_input(last_error)},
            ]
    raise ConceptGenerationError(last_error)


# --- Persistencia -----------------------------------------------------------------------


def create(db: DbSession, user: User, payload: DefinedStoryIn | ConceptStoryIn) -> StoryBlueprint:
    if isinstance(payload, DefinedStoryIn):
        texts = (payload.title, payload.name, payload.personality, payload.speakingStyle,
                 payload.setting, payload.tone, payload.backstory, payload.hook)
    else:
        texts = (payload.premise, payload.tone)
    rejection = moderation.check_user_text(*texts)
    if rejection:
        raise ContentRejectedError(rejection)

    count = db.scalar(
        select(func.count()).where(StoryBlueprint.owner_id == user.id, StoryBlueprint.deleted_at.is_(None))
    )
    if count >= MAX_BLUEPRINTS_PER_USER:
        raise BlueprintLimitError(LIMIT_MESSAGE)

    blueprint_id = uuid.uuid4().hex
    if isinstance(payload, DefinedStoryIn):
        profile, hook = build_defined_profile(payload, blueprint_id)
        title, premise = payload.title, None
    else:
        # Antes de escribir nada: si el modelo falla no queda una fila a medias.
        title, hook, profile = generate_concept(payload.premise, payload.tone, blueprint_id)
        premise = payload.premise

    blueprint = StoryBlueprint(
        id=blueprint_id,
        owner_id=user.id,
        mode=payload.mode,
        title=title,
        hook=hook,
        premise=premise,
        # Sin `free_first_read`: en los propios manda la columna, no el perfil.
        profile=profile.model_dump(mode="json", exclude={"free_first_read"}),
        is_public=payload.isPublic,
        free_first_read=payload.freeFirstRead,
        adult=payload.adult,
        published_at=_now() if payload.isPublic else None,
    )
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    return blueprint


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _alive():
    return StoryBlueprint.deleted_at.is_(None)


def list_owned(db: DbSession, user: User) -> list[StoryBlueprint]:
    return list(
        db.scalars(
            select(StoryBlueprint)
            .where(StoryBlueprint.owner_id == user.id, _alive())
            .order_by(StoryBlueprint.created_at.desc(), StoryBlueprint.id)
        )
    )


def get_owned(db: DbSession, user_id: str, blueprint_id: str) -> StoryBlueprint | None:
    blueprint = db.get(StoryBlueprint, blueprint_id)
    # Igual que las historias: la de otra cuenta, para quien pregunta, no existe.
    if blueprint is None or blueprint.owner_id != user_id or blueprint.deleted_at is not None:
        return None
    return blueprint


def update_settings(
    db: DbSession,
    blueprint: StoryBlueprint,
    *,
    is_public: bool | None = None,
    free_first_read: bool | None = None,
    adult: bool | None = None,
) -> StoryBlueprint:
    if is_public is not None:
        if is_public and not blueprint.is_public:
            blueprint.published_at = _now()
        blueprint.is_public = is_public
    if free_first_read is not None:
        blueprint.free_first_read = free_first_read
    if adult is not None:
        blueprint.adult = adult
    db.commit()
    db.refresh(blueprint)
    return blueprint


def delete_owned(db: DbSession, blueprint: StoryBlueprint) -> None:
    """Borra el personaje para su dueño sin romper las partidas de otras cuentas.

    - Las partidas del propio autor se borran con él: es quien decide borrar, y así
      "borrar" significa lo mismo que antes de que existieran las historias públicas.
    - Si otras cuentas tienen partidas con este personaje, la fila se queda con
      `deleted_at` (y deja de ser pública): el perfil es lo que el LLM necesita para
      continuar, y copiarlo a cada partida duplicaría secretos por toda la base. Para el
      autor, Explorar y cualquier partida nueva, ya no existe.
    - Si nadie más la jugó, se borra de verdad: no se guarda un perfil que no usa nadie.
    """
    ref = character_ref(blueprint.id)
    own_story_ids = select(Story.id).where(Story.user_id == blueprint.owner_id, Story.character_id == ref)
    for model in (Message, MemoryFact, StoryEvent):
        db.execute(delete(model).where(model.story_id.in_(own_story_ids)))
    db.execute(delete(Story).where(Story.user_id == blueprint.owner_id, Story.character_id == ref))

    played_by_others = db.scalar(select(func.count()).where(Story.character_id == ref))
    if played_by_others:
        blueprint.deleted_at = _now()
        blueprint.is_public = False
    else:
        db.delete(blueprint)
    db.commit()


def blueprint_for(db: DbSession, character_id: str) -> StoryBlueprint | None:
    blueprint_id = _blueprint_id(character_id)
    return db.get(StoryBlueprint, blueprint_id) if blueprint_id else None


def profile_of(blueprint: StoryBlueprint) -> CharacterProfile:
    return CharacterProfile.model_validate(blueprint.profile)


def _blueprint_id(character_id: str) -> str | None:
    return character_id[len(CUSTOM_PREFIX):] if character_id.startswith(CUSTOM_PREFIX) else None


def resolve_profile(db: DbSession, user_id: str, character_id: str) -> CharacterProfile | None:
    """Perfil con el que esa cuenta puede *empezar* una partida: uno suyo o uno público
    de otra, sin borrar. Uno privado ajeno es None, igual que uno que no existe."""
    blueprint_id = _blueprint_id(character_id)
    blueprint = db.get(StoryBlueprint, blueprint_id) if blueprint_id else None
    if blueprint is None or blueprint.deleted_at is not None:
        return None
    if blueprint.owner_id != user_id and not blueprint.is_public:
        return None
    return profile_of(blueprint)


def resolve_story_profile(db: DbSession, character_id: str) -> CharacterProfile | None:
    """Perfil para *continuar* una partida que ya existe.

    No mira ni visibilidad ni borrado: la partida se creó cuando su dueño tenía acceso, y
    que el autor la despublique o la borre después no se la quita.
    """
    blueprint_id = _blueprint_id(character_id)
    blueprint = db.get(StoryBlueprint, blueprint_id) if blueprint_id else None
    return profile_of(blueprint) if blueprint else None


def to_out(blueprint: StoryBlueprint) -> CustomStoryOut:
    profile = profile_of(blueprint)
    return CustomStoryOut(
        id=blueprint.id,
        characterId=character_ref(blueprint.id),
        mode=blueprint.mode,
        title=blueprint.title,
        hook=blueprint.hook,
        premise=blueprint.premise,
        tone=profile.tono,
        definition=_definition(blueprint, profile),
        isPublic=blueprint.is_public,
        freeFirstRead=blueprint.free_first_read,
        adult=blueprint.adult,
        publishedAt=blueprint.published_at,
        createdAt=blueprint.created_at,
    )


def _definition(blueprint: StoryBlueprint, profile: CharacterProfile) -> DefinitionOut | None:
    if blueprint.mode != "definida":
        return None
    return DefinitionOut(
        name=profile.nombre,
        age=profile.edad,
        personality=", ".join(profile.personalidad),
        speakingStyle=profile.forma_de_hablar.registro,
        setting=profile.escenario_inicial,
        tone=profile.tono,
        backstory=profile.trasfondo,
    )


def card_out(blueprint: StoryBlueprint, author: User, viewer_id: str) -> StoryCardOut:
    """Tarjeta pública de una historia (Explorar y estantería "Publicadas").

    Qué sale depende de quién escribió cada cosa:
    - **definida**: todo lo del formulario (`definition`), que lo escribió el autor y lo
      publicó sabiendo que se ve.
    - **concepto**: título, gancho y tono. El tono lo escribió el autor; título y gancho
      existen justo para anunciar la historia. La premisa no sale: cuenta la trama que el
      LLM convirtió en secretos. El perfil (nombre, mundo, secretos) lo inventó el LLM y se
      descubre jugando, así que no sale nunca, ni siquiera a su autor.
    """
    profile = profile_of(blueprint)
    return StoryCardOut(
        id=blueprint.id,
        characterId=character_ref(blueprint.id),
        mode=blueprint.mode,
        title=blueprint.title,
        hook=blueprint.hook,
        tone=profile.tono,
        definition=_definition(blueprint, profile),
        author=AuthorOut(
            displayName=author.display_name,
            handle=author.handle,
            avatarUrl=media_service.url_for(author.avatar_path),
        ),
        isMine=blueprint.owner_id == viewer_id,
        publishedAt=blueprint.published_at,
        adult=blueprint.adult,
    )


def list_public(
    db: DbSession,
    *,
    limit: int,
    offset: int,
    mode: str | None = None,
    owner_id: str | None = None,
    include_adult: bool = False,
) -> tuple[list[tuple[StoryBlueprint, User]], bool]:
    """Públicas y sin borrar, las publicadas más recientemente primero. Devuelve también
    si hay más: se pide una fila de más en vez de contar la tabla entera."""
    query = (
        select(StoryBlueprint, User)
        .join(User, User.id == StoryBlueprint.owner_id)
        .where(StoryBlueprint.is_public.is_(True), _alive())
    )
    if mode:
        query = query.where(StoryBlueprint.mode == mode)
    if owner_id:
        query = query.where(StoryBlueprint.owner_id == owner_id)
    if not include_adult:
        query = query.where(StoryBlueprint.adult.is_(False))
    rows = db.execute(
        query.order_by(StoryBlueprint.published_at.desc(), StoryBlueprint.id.desc())
        .limit(limit + 1)
        .offset(offset)
    ).all()
    return [(b, u) for b, u in rows[:limit]], len(rows) > limit


def character_out(blueprint: StoryBlueprint) -> CharacterOut:
    base = {
        "id": character_ref(blueprint.id),
        "origin": "propia",
        "mode": blueprint.mode,
        "title": blueprint.title,
        "hook": blueprint.hook,
        "adult": blueprint.adult,
    }
    if blueprint.mode == "concepto":
        return CharacterOut(**base, name=None, age=None, tagline=None, traits=None, scenario=None)
    profile = profile_of(blueprint)
    return CharacterOut(
        **base,
        name=profile.nombre,
        age=profile.edad,
        tagline=profile.tagline,
        traits=list(profile.personalidad),
        scenario=profile.escenario_inicial,
    )
