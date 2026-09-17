"""Extractor: lo que devuelve el LLM pasa por listas blancas antes de guardarse."""
import json

from app.llm import router as llm_router
from app.story import memory


def test_extrae_hechos_y_senales_validos():
    raw = json.dumps(
        {
            "hechos": {"nombre": "Marta", "gustos": ["el cine de los ochenta"], "promesas": ["volver el sábado"]},
            "senales": ["humor", "interes_personal"],
        }
    )
    result = memory.parse_extraction(f"Claro, aquí tienes: {raw}")
    assert result.facts == {
        "nombre": ["Marta"],
        "gustos": ["el cine de los ochenta"],
        "promesas": ["volver el sábado"],
    }
    assert result.signals == ["humor", "interes_personal"]


def test_rechaza_claves_no_permitidas():
    raw = json.dumps(
        {
            "hechos": {"nombre": "Marta", "direccion": "Calle Mayor 3", "afinidad": 100},
            "senales": [],
            "fase": "desenlace",
            "afinidad": 100,
        }
    )
    result = memory.parse_extraction(raw)
    assert result.facts == {"nombre": ["Marta"]}
    assert result.signals == []


def test_rechaza_senales_fuera_de_la_lista_blanca():
    raw = json.dumps({"hechos": {}, "senales": ["amor_verdadero", "cumplido", 42, "cumplido", "humor", "grosero", "escucha_activa"]})
    result = memory.parse_extraction(raw)
    assert result.signals == ["cumplido", "humor", "grosero"]  # sin duplicados y con tope


def test_valores_invalidos_se_descartan_sin_perder_los_validos():
    raw = json.dumps(
        {
            "hechos": {
                "nombre": "M4rt@ <script>",
                "gustos": ["ok", "los paseos junto al mar", "x" * 500, 7],
                "aficiones": "la escalada",
            },
            "senales": "cumplido",
        }
    )
    result = memory.parse_extraction(raw)
    assert "nombre" not in result.facts
    assert result.facts["gustos"] == ["los paseos junto al mar"]
    assert result.facts["aficiones"] == ["la escalada"]
    assert result.signals == []


def test_no_guarda_datos_sensibles_como_hechos():
    raw = json.dumps({"hechos": {"gustos": ["escribirme a marta@example.com", "llamar al 612345678"]}})
    assert memory.parse_extraction(raw).facts == {}


def test_basura_o_json_roto_no_rompe_nada():
    assert memory.parse_extraction(None) == memory.EMPTY
    assert memory.parse_extraction("no hay json aquí") == memory.EMPTY
    assert memory.parse_extraction("{roto: ") == memory.EMPTY
    assert memory.parse_extraction("[1, 2]") == memory.EMPTY


def test_si_el_llm_no_esta_disponible_devuelve_vacio(monkeypatch):
    def caido(*args, **kwargs):
        raise llm_router.LLMUnavailableError("apagado")

    monkeypatch.setattr(llm_router, "generate", caido)
    assert memory.extract_turn("Hola", "Me llamo Marta") == memory.EMPTY
