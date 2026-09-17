"""Estado de la historia: fase y afinidad, calculados por código.

El LLM nunca decide aquí. Lo más que aporta es una lista de señales de un conjunto
cerrado (`SIGNAL_WEIGHTS`), ya validada; los pesos, los umbrales y las transiciones son
reglas explícitas de este módulo, cada una con una razón legible que se guarda.

Todo son funciones puras sobre una lista de eventos, para poder probarlas sin base de
datos y para que cambiar un peso recalcule cualquier historia existente igual.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum


class Phase(StrEnum):
    CONOCERSE = "conocerse"
    CONFIANZA = "confianza"
    TENSION = "tension"
    CONFLICTO = "conflicto"
    DESENLACE = "desenlace"


PHASE_ORDER = list(Phase)

PHASE_LABELS = {
    Phase.CONOCERSE: "Conocerse",
    Phase.CONFIANZA: "Confianza",
    Phase.TENSION: "Tensión",
    Phase.CONFLICTO: "Conflicto",
    Phase.DESENLACE: "Desenlace",
}

# Instrucciones de escena que se inyectan en el system prompt según la fase.
PHASE_SCENE = {
    Phase.CONOCERSE: (
        "Primeros compases. Curiosidad, presentaciones, pequeñas bromas. El personaje "
        "aún guarda las distancias: nada de declaraciones ni confidencias profundas."
    ),
    Phase.CONFIANZA: (
        "Ya hay complicidad. El personaje comparte recuerdos y gustos, pregunta por la "
        "vida de la otra persona y recuerda lo que le contó. Coqueteo suave y respetuoso."
    ),
    Phase.TENSION: (
        "La atracción es evidente y ninguno lo ha dicho. Silencios cargados, miradas, "
        "algún roce casual. El personaje duda si dar un paso. Si hay un beso, se describe "
        "con delicadeza y sin detalle físico explícito."
    ),
    Phase.CONFLICTO: (
        "Surge un malentendido o un miedo del pasado del personaje que lo hace retroceder. "
        "No es un castigo: es un obstáculo que se resuelve hablando con honestidad."
    ),
    Phase.DESENLACE: (
        "Cierre del arco. Según cómo haya ido, reconciliación y compromiso de seguir "
        "viéndose, o una despedida cálida y agridulce. Tono sereno, sin melodrama."
    ),
}

# --- Pesos --------------------------------------------------------------------------

BASE_AFFINITY = 20
MIN_AFFINITY, MAX_AFFINITY = 0, 100

# Señales que el extractor puede devolver. Cualquier otra se descarta antes de llegar
# aquí; que esté en este dict es lo que la hace existir.
SIGNAL_WEIGHTS: dict[str, int] = {
    "cumplido": 3,
    "interes_personal": 2,
    "humor": 2,
    "escucha_activa": 3,
    "vulnerabilidad": 4,
    "respeta_limite": 2,
    "desinteres": -4,
    "grosero": -8,
    "presiona_limite": -6,
}
# Tope por turno: diez cumplidos seguidos en un mensaje no valen diez veces.
MAX_SIGNALS_PER_TURN = 3

# Constancia: un punto cada dos turnos, hasta 15. Volver cuenta, pero no compra la historia.
TURN_BONUS_EVERY = 2
TURN_BONUS_CAP = 15


# Intenciones que puede tener una sugerencia, con su peso en la afinidad. Las sugerencias
# por escena las redacta el LLM, pero solo elige la intención de esta lista: el peso lo pone
# el código. El evento `decision` se guarda con la intención, así que cambiar un peso aquí
# recalcula también las historias existentes.
INTENT_WEIGHTS: dict[str, int] = {
    "preguntar": 2,
    "escuchar": 2,
    "presentarse": 1,
    "humor": 1,
    "cumplido": 2,
    "compartir": 3,
    "sincerarse": 4,
    "proponer_plan": 3,
    "dar_espacio": 2,
    "cambiar_tema": -1,
    "pedir_perdon": 4,
    "reconciliar": 5,
    "tomar_distancia": -3,
    "agradecer": 1,
    "hablar_futuro": 2,
    "despedirse": 0,
}

# Ids de las sugerencias fijas anteriores a las intenciones: siguen en eventos ya guardados.
LEGACY_CHOICE_INTENTS: dict[str, str] = {
    "preguntar_trabajo": "preguntar",
    "hacer_broma": "humor",
    "compartir_recuerdo": "compartir",
    "proponer_paseo": "proponer_plan",
    "preguntar_sueno": "preguntar",
    "proponer_futuro": "hablar_futuro",
}


@dataclass(frozen=True)
class QuickChoice:
    intent: str
    label: str
    message: str

    @property
    def weight(self) -> int:
        return INTENT_WEIGHTS[self.intent]


# Sugerencias por fase: la reserva cuando el LLM no da sugerencias válidas para la escena.
QUICK_CHOICES: dict[Phase, tuple[QuickChoice, ...]] = {
    Phase.CONOCERSE: (
        QuickChoice("preguntar", "Preguntar por su oficio", "Cuéntame, ¿cómo acabaste dedicándote a esto?"),
        QuickChoice("humor", "Romper el hielo con humor", "*Sonrío* Tengo la sensación de que no es la primera vez que te pasa algo así."),
        QuickChoice("presentarse", "Presentarte", "Por cierto, no me he presentado. Es un placer conocerte."),
    ),
    Phase.CONFIANZA: (
        QuickChoice("compartir", "Compartir un recuerdo", "Esto me recuerda a algo que me pasó hace tiempo... ¿te lo cuento?"),
        QuickChoice("proponer_plan", "Proponer un paseo", "¿Te apetece dar un paseo cuando acabes?"),
        QuickChoice("preguntar", "Preguntar por sus sueños", "Si pudieras cambiar una cosa de tu vida mañana, ¿cuál sería?"),
    ),
    Phase.TENSION: (
        QuickChoice("sincerarse", "Sincerarte", "Llevo un rato queriendo decirte algo y no sé muy bien cómo."),
        QuickChoice("dar_espacio", "Darle espacio", "No hace falta que digas nada ahora. Estoy bien así."),
        QuickChoice("cambiar_tema", "Cambiar de tema", "*Carraspeo* Bueno... ¿y qué tal el día?"),
    ),
    Phase.CONFLICTO: (
        QuickChoice("reconciliar", "Buscar la reconciliación", "No quiero que esto se quede así. ¿Podemos hablarlo con calma?"),
        QuickChoice("pedir_perdon", "Pedir perdón", "Creo que te he hecho daño sin querer. Lo siento de verdad."),
        QuickChoice("tomar_distancia", "Tomar distancia", "Quizá necesitemos un poco de tiempo cada uno."),
    ),
    Phase.DESENLACE: (
        QuickChoice("hablar_futuro", "Hablar del futuro", "¿Y ahora qué? Porque yo no quiero que esto acabe aquí."),
        QuickChoice("agradecer", "Dar las gracias", "Gracias por dejarme entrar en tu mundo."),
        QuickChoice("despedirse", "Despedirse", "*Te abrazo* Cuídate mucho, ¿vale?"),
    ),
}


def choices_for(phase: Phase) -> tuple[QuickChoice, ...]:
    return QUICK_CHOICES[phase]


# --- Eventos y estado -----------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    kind: str  # "turno" | "senal" | "decision" | "transicion" | "ajuste"
    name: str
    turn: int
    # Solo en "ajuste" (herramientas de dev): puntos que suma o resta tal cual.
    weight: int | None = None


@dataclass(frozen=True)
class StoryState:
    phase: Phase
    affinity: int
    turn_count: int
    turns_in_phase: int
    signals: Counter = field(default_factory=Counter)
    decisions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Transition:
    from_phase: Phase
    to_phase: Phase
    reason: str


def _decision_weight(name: str) -> int:
    return INTENT_WEIGHTS.get(LEGACY_CHOICE_INTENTS.get(name, name), 0)


def compute_affinity(events: list[Event]) -> int:
    """Afinidad 0-100 a partir de los eventos. Determinista y sin estado.

    Se recorta a 0-100 después de cada evento y no solo al final. Con el recorte al final,
    tres turnos groseros dejaban una deuda invisible (la partida enseñaba 0 y por debajo
    iba por -17) que había que pagar antes de que un buen turno se notara; y al revés, lo
    ganado por encima de 100 servía de colchón oculto. Lo que se ve es lo que hay.
    """
    score = BASE_AFFINITY
    per_turn: Counter = Counter()
    turns = 0

    def clamp(value: int) -> int:
        return max(MIN_AFFINITY, min(MAX_AFFINITY, value))

    for ev in events:
        if ev.kind == "turno":
            turns += 1
            # Constancia: el punto se suma en el turno que lo gana, no todo al final.
            if turns % TURN_BONUS_EVERY == 0 and turns // TURN_BONUS_EVERY <= TURN_BONUS_CAP:
                score = clamp(score + 1)
        elif ev.kind == "senal" and ev.name in SIGNAL_WEIGHTS:
            if per_turn[ev.turn] >= MAX_SIGNALS_PER_TURN:
                continue
            per_turn[ev.turn] += 1
            score = clamp(score + SIGNAL_WEIGHTS[ev.name])
        elif ev.kind == "decision":
            score = clamp(score + _decision_weight(ev.name))
        elif ev.kind == "ajuste" and ev.weight is not None:
            score = clamp(score + ev.weight)
    return score


def derive_state(events: list[Event]) -> StoryState:
    """Reconstruye el estado entero. La fase sale de las transiciones registradas, no de
    volver a evaluar las reglas: una regla cambiada no debe reescribir el pasado."""
    phase = Phase.CONOCERSE
    phase_start_turn = 0
    turns = 0
    signals: Counter = Counter()
    decisions: set[str] = set()
    for ev in events:
        if ev.kind == "turno":
            turns += 1
        elif ev.kind == "senal":
            signals[ev.name] += 1
        elif ev.kind == "decision":
            decisions.add(ev.name)
        elif ev.kind == "transicion" and ev.name in Phase._value2member_map_:
            phase = Phase(ev.name)
            phase_start_turn = ev.turn
            # Las reglas miran las decisiones de la fase en curso: un "perdón" de hace dos
            # capítulos no resuelve el conflicto de ahora.
            decisions.clear()
    return StoryState(
        phase=phase,
        affinity=compute_affinity(events),
        turn_count=turns,
        turns_in_phase=turns - phase_start_turn,
        signals=signals,
        decisions=frozenset(decisions),
    )


def next_transition(state: StoryState) -> Transition | None:
    """Reglas de avance. Como mucho un paso por turno y nunca hacia atrás."""
    p, a, t = state.phase, state.affinity, state.turns_in_phase

    if p is Phase.CONOCERSE and state.turn_count >= 4 and a >= 35:
        return Transition(p, Phase.CONFIANZA, f"Cuatro turnos o más y afinidad {a} (≥35).")

    if p is Phase.CONFIANZA and t >= 5 and a >= 55 and state.signals["vulnerabilidad"] >= 1:
        return Transition(
            p, Phase.TENSION, f"Cinco turnos en confianza, afinidad {a} (≥55) y alguien se ha abierto."
        )

    if p is Phase.TENSION and t >= 5 and a >= 65:
        return Transition(p, Phase.CONFLICTO, f"La tensión lleva cinco turnos con afinidad {a} (≥65).")

    if p is Phase.CONFLICTO and t >= 3:
        if "reconciliar" in state.decisions or "pedir_perdon" in state.decisions:
            return Transition(p, Phase.DESENLACE, "Se ha buscado la reconciliación.")
        if a >= 75:
            return Transition(p, Phase.DESENLACE, f"El vínculo aguanta el conflicto (afinidad {a} ≥75).")

    # Salida anticipada: una relación que se ha roto no sigue fingiendo que avanza.
    if p is not Phase.DESENLACE and state.turn_count >= 6 and a < 10:
        return Transition(p, Phase.DESENLACE, f"La afinidad ha caído a {a} (<10): la historia se cierra.")

    return None


def affinity_band(affinity: int) -> str:
    """Descripción para el prompt. El número exacto no le sirve al modelo para escribir."""
    if affinity < 20:
        return "distante: el personaje está dolido o incómodo"
    if affinity < 40:
        return "cordial: simpatía prudente"
    if affinity < 60:
        return "cercana: se nota que disfruta de la conversación"
    if affinity < 80:
        return "muy cercana: hay complicidad y ternura"
    return "íntima: confianza plena, sin perder el tono para todos los públicos"
