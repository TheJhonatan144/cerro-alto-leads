"""Bloque 3.1: D7.1, regla pura de cruce por nombre (tests de tabla)."""
import pytest

from app.normalizar import elegir_por_nombre, palabras_nombre


@pytest.mark.parametrize("nombre, esperado", [
    ("Carla Mendoza", ("carla", "mendoza")),
    ("  CARLA   Mendóza ", ("carla", "mendoza")),
    ("Juan de la Cruz", ("juan", "cruz")),
    ("María de los Ángeles del Río y Paz", ("maria", "angeles", "rio", "paz")),
    ("", ()),
    (None, ()),
    ("De La", ()),
])
def test_palabras_nombre(nombre, esperado):
    assert palabras_nombre(nombre) == esperado


@pytest.mark.parametrize("lead, candidatos, esperado", [
    # 2) igualdad exacta de palabras (sin importar orden, tildes ni partículas)
    ("Carla Mendoza", ["Carla Mendoza"], 0),
    ("Mendoza Carla", ["Carla Mendoza"], 0),
    ("María López", ["maria lopez"], 0),
    ("Juan de la Cruz", ["Juan Cruz"], 0),
    ("Juan Perez", ["Juan Perez Lopez", "Juan Perez"], None),       # D7.2: dos compatibles
    ("Juan Perez", ["Juan Perez", "juan pérez"], None),            # dos exactas
    # 3) el más corto contenido en el más largo, con al menos 2 palabras
    ("Carla Mendoza", ["Carla Mendoza Ruiz"], 0),
    ("Carla Mendoza Ruiz", ["Carla Mendoza"], 0),                  # simétrico
    ("Carla Mendoza", ["Rosa Quishpe", "Ruiz Carla Mendoza"], 1),
    ("Juan Perez", ["Juan Perez Lopez", "Juan Perez Gomez"], None),
    ("Carla Mendoza Ruiz", ["Carla Ruiz Vera"], None),             # no contenido
    ("Carla Mendoza Ruiz", ["Carla"], None),                       # el corto tiene 1 palabra
    # 4) el lead vacío o de una palabra nunca confirma
    ("Carla", ["Carla"], None),
    ("Carla", ["Carla Mendoza"], None),
    ("", ["Carla Mendoza"], None),
    ("De la", ["Carla Mendoza"], None),
    ("Carla Mendoza", ["Rosa Quishpe"], None),
    ("Carla Mendoza", [], None),
    ("Carla Mendoza", [""], None),
])
def test_elegir_por_nombre(lead, candidatos, esperado):
    assert elegir_por_nombre(lead, candidatos) == esperado


def test_repeticion_de_palabras_cuenta():
    # "Carla Carla" no está contenido en "Carla Mendoza": las palabras se cuentan.
    assert elegir_por_nombre("Carla Carla", ["Carla Mendoza"]) is None
    assert elegir_por_nombre("Carla Mendoza", ["Carla Carla Mendoza"]) == 0


# --- Bloque 3.2: D7.2 ---------------------------------------------------------------------

@pytest.mark.parametrize("lead, candidatos, esperado", [
    ("Juan Pérez", ["Juan Pérez", "Juan Carlos Pérez"], None),   # padre e hijo, mismo contacto
    ("Juan Carlos Pérez", ["Juan Pérez", "Juan Carlos Pérez"], None),
    ("Juan Pérez", ["Juan Pérez", "Rosa Quishpe"], 0),             # una sola compatible
    ("Juan Pérez", ["Juan Pérez López", "Rosa Quishpe"], 0),
])
def test_d72_mas_de_una_compatible_es_ambigua(lead, candidatos, esperado):
    assert elegir_por_nombre(lead, candidatos) == esperado
