"""La máquina de estados es código puro: se prueba sin base de datos ni LLM."""
from app.story import state_machine as sm
from app.story.state_machine import Event, Phase


def turns(n: int, start: int = 1) -> list[Event]:
    return [Event("turno", "mensaje", t) for t in range(start, start + n)]


def test_sin_eventos_la_afinidad_es_la_base():
    assert sm.compute_affinity([]) == sm.BASE_AFFINITY


def test_la_afinidad_es_determinista_y_suma_los_pesos():
    events = turns(2) + [Event("senal", "cumplido", 1), Event("senal", "humor", 2)]
    esperado = sm.BASE_AFFINITY + 3 + 2 + 1  # +1 por cada dos turnos
    assert sm.compute_affinity(events) == esperado
    assert sm.compute_affinity(list(events)) == esperado


def test_una_senal_desconocida_no_puntua():
    """Aunque se colara en la base, solo existe lo que está en SIGNAL_WEIGHTS."""
    assert sm.compute_affinity([Event("senal", "declaracion_de_amor", 1)]) == sm.BASE_AFFINITY


def test_tope_de_senales_por_turno():
    events = [Event("senal", "cumplido", 1)] * 10
    assert sm.compute_affinity(events) == sm.BASE_AFFINITY + 3 * sm.MAX_SIGNALS_PER_TURN


def test_el_bono_por_turnos_tiene_tope():
    assert sm.compute_affinity(turns(500)) == sm.BASE_AFFINITY + sm.TURN_BONUS_CAP


def test_la_afinidad_se_queda_en_0_100():
    groserias = [Event("senal", "grosero", t) for t in range(1, 30)]
    assert sm.compute_affinity(groserias) == 0
    halagos = [Event("senal", "vulnerabilidad", t) for t in range(1, 60)]
    assert sm.compute_affinity(halagos) == 100


def test_las_decisiones_pesan_segun_la_tabla_del_codigo():
    assert sm.compute_affinity([Event("decision", "reconciliar", 1)]) == sm.BASE_AFFINITY + 5
    assert sm.compute_affinity([Event("decision", "inventada", 1)]) == sm.BASE_AFFINITY


def test_conocerse_no_avanza_sin_turnos_suficientes():
    events = turns(3) + [Event("senal", s, t) for t in range(1, 4) for s in ("vulnerabilidad", "cumplido")]
    state = sm.derive_state(events)
    assert state.affinity >= 35
    assert sm.next_transition(state) is None


def test_conocerse_pasa_a_confianza_con_razon_legible():
    events = turns(4) + [Event("senal", s, t) for t in range(1, 5) for s in ("cumplido", "humor")]
    state = sm.derive_state(events)
    transition = sm.next_transition(state)
    assert transition is not None
    assert (transition.from_phase, transition.to_phase) == (Phase.CONOCERSE, Phase.CONFIANZA)
    assert "afinidad" in transition.reason.lower()


def test_la_fase_sale_de_las_transiciones_registradas():
    events = turns(4) + [Event("transicion", "confianza", 4)] + turns(2, start=5)
    state = sm.derive_state(events)
    assert state.phase is Phase.CONFIANZA
    assert state.turns_in_phase == 2


def test_confianza_exige_que_alguien_se_haya_abierto():
    base = turns(4) + [Event("transicion", "confianza", 4)] + turns(6, start=5)
    halagos = [Event("senal", "escucha_activa", t) for t in range(1, 11)]
    sin_vulnerabilidad = sm.derive_state(base + halagos)
    assert sin_vulnerabilidad.affinity >= 55
    assert sm.next_transition(sin_vulnerabilidad) is None

    con = sm.derive_state(base + halagos + [Event("senal", "vulnerabilidad", 10)])
    assert sm.next_transition(con).to_phase is Phase.TENSION


def test_conflicto_se_resuelve_con_la_decision_de_reconciliar():
    events = (
        turns(3)
        + [Event("transicion", "conflicto", 3)]
        + turns(3, start=4)
        + [Event("decision", "reconciliar", 6)]
    )
    transition = sm.next_transition(sm.derive_state(events))
    assert transition.to_phase is Phase.DESENLACE


def test_una_relacion_rota_se_cierra():
    events = turns(6) + [Event("senal", "grosero", t) for t in range(1, 4)]
    transition = sm.next_transition(sm.derive_state(events))
    assert transition.to_phase is Phase.DESENLACE


def test_desenlace_es_final():
    events = turns(10) + [Event("transicion", "desenlace", 5)]
    assert sm.next_transition(sm.derive_state(events)) is None


def test_cada_fase_tiene_escena_y_sugerencias():
    for phase in Phase:
        assert sm.PHASE_SCENE[phase]
        assert 2 <= len(sm.choices_for(phase)) <= 3


def test_las_sugerencias_de_reserva_usan_intenciones_de_la_lista():
    for phase in Phase:
        intents = [c.intent for c in sm.choices_for(phase)]
        assert all(i in sm.INTENT_WEIGHTS for i in intents)
        assert len(set(intents)) == len(intents)


def test_el_peso_de_una_decision_sale_de_su_intencion():
    assert sm.compute_affinity([Event("decision", "pedir_perdon", 1)]) == sm.BASE_AFFINITY + sm.INTENT_WEIGHTS["pedir_perdon"]
    assert sm.compute_affinity([Event("decision", "tomar_distancia", 1)]) == sm.BASE_AFFINITY - 3


def test_las_decisiones_antiguas_por_id_siguen_pesando_igual():
    assert sm.compute_affinity([Event("decision", "preguntar_trabajo", 1)]) == sm.BASE_AFFINITY + 2
    assert sm.compute_affinity([Event("decision", "proponer_paseo", 1)]) == sm.BASE_AFFINITY + 3


def test_un_perdon_de_otra_fase_no_resuelve_el_conflicto():
    events = (
        turns(3)
        + [Event("decision", "pedir_perdon", 2), Event("transicion", "conflicto", 3)]
        + turns(3, start=4)
    )
    state = sm.derive_state(events)
    assert state.decisions == frozenset()
    assert sm.next_transition(state) is None


def _turnos(*turnos: list[tuple[str, str]]) -> list[sm.Event]:
    eventos = []
    for n, contenido in enumerate(turnos, start=1):
        eventos.append(sm.Event("turno", "mensaje", n))
        eventos.extend(sm.Event(kind, name, n) for kind, name in contenido)
    return eventos


def test_la_afinidad_no_acumula_deuda_por_debajo_de_cero():
    # Regresión de la partida real de lucia: tres turnos groseros la dejaron a 0 y, al
    # recortar solo al final, por debajo seguía en negativo y los buenos turnos no se notaban.
    groseros = [("senal", "grosero"), ("senal", "presiona_limite")]
    eventos = _turnos(groseros, groseros, groseros, groseros)
    assert sm.compute_affinity(eventos) == 0
    eventos.append(sm.Event("turno", "mensaje", 5))
    eventos.append(sm.Event("senal", "cumplido", 5))
    assert sm.compute_affinity(eventos) == sm.SIGNAL_WEIGHTS["cumplido"]


def test_la_afinidad_no_guarda_colchon_por_encima_de_100():
    eventos = [sm.Event("ajuste", "dev", 0, weight=150), sm.Event("senal", "grosero", 1)]
    assert sm.compute_affinity(eventos) == sm.MAX_AFFINITY + sm.SIGNAL_WEIGHTS["grosero"]


def test_ajuste_de_dev_suma_su_peso_y_sin_peso_no_cuenta():
    assert sm.compute_affinity([sm.Event("ajuste", "dev", 0, weight=15)]) == sm.BASE_AFFINITY + 15
    assert sm.compute_affinity([sm.Event("ajuste", "dev", 0)]) == sm.BASE_AFFINITY


def test_las_decisiones_de_fases_anteriores_siguen_contando_en_la_afinidad():
    # Las reglas de transición solo miran las decisiones de la fase en curso, pero la
    # afinidad no pierde lo ganado antes de una transición.
    eventos = [sm.Event("decision", "sincerarse", 1), sm.Event("transicion", "confianza", 1)]
    assert sm.compute_affinity(eventos) == sm.BASE_AFFINITY + sm.INTENT_WEIGHTS["sincerarse"]
    assert sm.derive_state(eventos).decisions == frozenset()
