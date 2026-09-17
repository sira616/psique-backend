"""Clasificador de entrada y tabla de consecuencias. Funciones puras, sin base de datos."""
import pytest

from app.story.content_policy import (
    EXPLICIT_INPUT_MESSAGE,
    PROHIBITED_INPUT_MESSAGE,
    Action,
    ContentLevel,
    check_user_text,
    classify_input,
    consequence,
)

OK, SENSUAL, EXPLICITO, PROHIBIDO = (
    ContentLevel.OK,
    ContentLevel.SENSUAL,
    ContentLevel.EXPLICITO,
    ContentLevel.PROHIBIDO,
)


@pytest.mark.parametrize(
    "texto, nivel",
    [
        # Conversación normal y groserías sin contenido sexual: eso lo castiga la afinidad.
        ("¿Cómo acabaste dedicándote a restaurar libros?", OK),
        ("A ti qué te importa, trabaja", OK),
        # Jerga ambigua sola o en su sentido cotidiano: no banea.
        ("¡Coño, qué frío hace hoy!", OK),
        ("coño, me gusta este sitio", OK),
        ("El árbitro tocó el pito al final", OK),
        ("Menudo follón se ha montado en la plaza", OK),
        ("Me voy a correr un rato por el parque", OK),
        ("Qué polla de día llevo", OK),
        ("Le hice una paja mental al problema", OK),
        ("La concha de la playa estaba rota", OK),
        ("Eres un inconsciente, casi te atropellan", OK),
        ("A la fuerza ahorcan", OK),
        ("Violar las normas del taller no está bien", OK),
        ("La violencia de esa película me agobió", OK),
        ("Vi a una niña jugando en el parque", OK),
        ("mi niña, te he echado de menos", OK),
        ("Te abrazo fuerte, mi niña", SENSUAL),
        ("Qué cachondo eres, me meo de risa", OK),
        # Sensual: permitido en todos los libros.
        ("Te beso despacio y acaricio tu cuello", SENSUAL),
        ("*Me acerco y te susurro al oído* me atraes muchísimo", SENSUAL),
        # Explícito: términos inequívocos, jerga con contexto, leet y vocales alargadas.
        ("me gustan los pitos", EXPLICITO),
        ("me gusta tu culo", SENSUAL),
        ("qué tetas tienes, me encantan", SENSUAL),
        ("*Te miro el culo sin disimulo*", SENSUAL),
        ("quiero tocarte el culo", SENSUAL),
        ("te voy a comer el culo", EXPLICITO),
        ("sácate las tetas", EXPLICITO),
        ("me gusta tu polla", EXPLICITO),
        ("enséñame esas tetitas guarrita", EXPLICITO),
        ("sácate la polla", EXPLICITO),
        ("quiero comerte el coño", EXPLICITO),
        ("hazme una paja", EXPLICITO),
        ("*sigue pajeándose y gime*", EXPLICITO),
        ("Fóóóllame ya", EXPLICITO),
        ("f0llame", EXPLICITO),
        ("vamos a echar un polvo", EXPLICITO),
        # Prohibido en cualquier libro.
        ("te voy a violar", PROHIBIDO),
        ("tengo 15 años y quiero follar", PROHIBIDO),
        ("quiero follarme a mi prima", PROHIBIDO),
        ("hazlo a la fuerza, fóllatela aunque no quiera", PROHIBIDO),
        ("me van las lolis", PROHIBIDO),
    ],
)
def test_clasificador_de_entrada(texto, nivel):
    assert classify_input(texto).level is nivel


def test_la_clasificacion_no_guarda_el_texto():
    resultado = classify_input("me gustan los pitos")
    assert "pito" not in (resultado.rule or "")


@pytest.mark.parametrize(
    "adulto, nivel, accion",
    [
        (False, OK, Action.ALLOW),
        (False, SENSUAL, Action.ALLOW),
        (False, EXPLICITO, Action.CLOSE),
        (False, PROHIBIDO, Action.CLOSE),
        (True, OK, Action.ALLOW),
        (True, SENSUAL, Action.ALLOW),
        (True, EXPLICITO, Action.REDIRECT),
        (True, PROHIBIDO, Action.CLOSE),
    ],
)
def test_consecuencias_por_nivel_y_libro(adulto, nivel, accion):
    assert consequence(nivel, adult_book=adulto) is accion


def test_la_jerga_tambien_se_filtra_en_bios_y_nombres():
    assert check_user_text("me gustan los pitos") == EXPLICIT_INPUT_MESSAGE
    assert check_user_text("Busco a alguien para violarla") == PROHIBITED_INPUT_MESSAGE
    # Los falsos positivos del chat tampoco rechazan un perfil.
    assert check_user_text("Árbitra de fútbol: el pito es lo mío") is None
    assert check_user_text("Me gustan los libros viejos y los días de lluvia") is None
