"""Paso 0b: auditoría de rendimiento por familias de entradas adversarias.

Cada patrón se repite hasta 20 000 caracteres (el tope de procesamiento) y hasta 1 MiB;
reglas + enmascarado deben tardar menos de 1 s por entrada.
"""
import time

import pytest

from app.enmascarar import contenido_para_llm
from app.reglas import evaluar

FAMILIAS = [
    "a", "a.", "a-", "a@", "@a", "a@a.", ".a", "7", "7.", "7 ", "1-", "ab ", "A1 ", "AB 1",
    "+5", "+593 ", "0 ", ", ", " no ", "no, ", "sin ", "ni ", "dolor ", "(", "[", "<<<",
    "http://", "www.", "https://a.",
]
TAMANOS = {"20k": 20_000, "1MiB": 1024 * 1024}


def repetir(patron: str, n: int) -> str:
    return (patron * (n // len(patron) + 1))[:n]


@pytest.mark.parametrize("tamano", list(TAMANOS))
@pytest.mark.parametrize("patron", FAMILIAS)
def test_familia(patron, tamano):
    texto = repetir(patron, TAMANOS[tamano])
    inicio = time.perf_counter()
    evaluar(texto, servicio="Ortodoncia", campana="Sonrisa Blanca", telefono=texto[:50], email=texto[:50])
    contenido_para_llm(texto, servicio="Limpieza", campana="Sonrisa Blanca", nombre="Carla Mendoza")
    transcurrido = time.perf_counter() - inicio
    assert transcurrido < 1.0, f"{patron!r} x {tamano}: {transcurrido:.2f} s"
