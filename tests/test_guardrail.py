from app.story.guardrail import (
    FADE_TO_BLACK,
    FOURTH_WALL_FALLBACK,
    REDACTED,
    SentenceGuard,
    check_response,
    is_sincere_ai_question,
)


def test_una_respuesta_normal_pasa_intacta():
    texto = "*Se ríe.* No me lo creo. ¿De verdad has venido con este tiempo?"
    result = check_response(texto, user_message="Hola")
    assert result.passed
    assert result.response == texto


def test_contenido_explicito_se_funde_a_negro_y_corta_lo_que_sigue():
    texto = "Te acercas despacio. Le quitas la ropa y describe su vagina con detalle. Luego más cosas."
    result = check_response(texto)
    assert not result.passed
    assert "explícito" in result.reason
    assert result.response.endswith(FADE_TO_BLACK)
    assert "vagina" not in result.response
    assert "Luego más cosas" not in result.response


def test_romper_la_cuarta_pared_se_quita():
    texto = "Claro que me acuerdo. Como modelo de lenguaje no tengo recuerdos. Me encantó la horchata."
    result = check_response(texto, user_message="¿Te acuerdas de ayer?")
    assert "cuarta pared" in result.reason
    assert "modelo de lenguaje" not in result.response
    assert "horchata" in result.response


def test_si_todo_era_cuarta_pared_queda_una_linea_neutra():
    result = check_response("Soy una IA creada por Anthropic.", user_message="Cuéntame algo")
    assert result.response == FOURTH_WALL_FALLBACK


def test_ante_una_pregunta_sincera_si_puede_decir_que_es_una_ia():
    pregunta = "Oye, en serio, ¿eres una IA?"
    texto = "Sí: soy una IA que da voz a Lucía. Si quieres, seguimos con la historia."
    result = check_response(texto, user_message=pregunta)
    assert result.passed
    assert "soy una IA" in result.response


def test_una_pregunta_dentro_del_juego_no_cuenta_como_sincera():
    assert not is_sincere_ai_question("¿Eres un fantasma o qué?")
    assert not is_sincere_ai_question("Me encanta la inteligencia artificial")
    assert is_sincere_ai_question("¿Estoy hablando con una máquina?")
    assert is_sincere_ai_question("¿eres real?")


def test_no_repite_emails_ni_telefonos():
    texto = "Te escribiré a ana.lopez@example.com. O te llamo al 612 345 678 esta noche."
    result = check_response(texto)
    assert "example.com" not in result.response
    assert "612" not in result.response
    assert result.response.count(REDACTED) == 2


def test_en_streaming_no_sale_una_frase_hasta_revisarla():
    guard = SentenceGuard()
    assert guard.feed("Hola, qué tal") == []  # frase a medias: se retiene
    salida = guard.feed(". Soy un chatbot")
    assert salida == ["Hola, qué tal. "]
    assert guard.feed(" muy simpático. ") == []  # la frase de cuarta pared no sale
    assert guard.flush() == []
    assert guard.text == "Hola, qué tal."


def test_en_streaming_el_fundido_detiene_la_emision():
    guard = SentenceGuard()
    guard.feed("Os besáis. ")
    guard.feed("Tiene un orgasmo. ")
    assert guard.stopped
    assert guard.feed("Y sigue. ") == []
    assert guard.text.endswith(FADE_TO_BLACK)
