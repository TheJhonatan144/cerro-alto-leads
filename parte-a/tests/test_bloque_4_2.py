"""Bloque 4.2: documentos, negación en listas, correos con espacios, NFC y rendimiento."""
import time

import pytest

from app.enmascarar import contenido_para_llm, enmascarar
from app.normalizar import MAX_TEXTO_PROCESADO
from app.reglas import evaluar


# --- 1) Documentos alfanuméricos -------------------------------------------------------------

@pytest.mark.parametrize("texto, dato", [
    ("pasaporte ABC123456", "ABC123456"),
    ("doc 123456AB", "123456AB"),
    ("id A-123456", "A-123456"),
    ("pasaporte AB 123456", "AB 123456"),
    ("ruc 1234567-AB", "1234567-AB"),
    ("cédula 12345678 XYZ", "12345678 XYZ"),
])
def test_42_documentos(texto, dato):
    t = enmascarar(texto)
    assert dato not in t and "[DOCUMENTO]" in t


@pytest.mark.parametrize("texto, esperado", [
    ("la cita a las 17:00", "la cita a las 17:00"),
    ("son 30 min", "son 30 min"),
    ("cuesta USD 180", "cuesta USD 180"),
    ("quiero 2 citas", "quiero 2 citas"),
    ("mi número 593 99 123 4567", "mi número [NUMERO]"),
    ("llama al 0 9 9 1 2 3 4 5 6 7", "llama al [NUMERO]"),
])
def test_42_se_conserva_lo_del_41(texto, esperado):
    assert enmascarar(texto) == esperado


# --- 2) Negación en enumeraciones -------------------------------------------------------------------

@pytest.mark.parametrize("mensaje", [
    "Sin dolor, hinchazón ni fiebre",
    "No tengo hinchazón, inflamación ni dolor",
    "no tengo dolor, ni hinchazón",
    "Sin dolor, hinchazón, inflamación, fiebre",
    "No tengo dolor de muela, hinchazón ni sangrado abundante",
])
def test_42_negacion_en_listas_no_es_urgencia(mensaje):
    assert "urgencia_clinica" not in evaluar(mensaje).senales


@pytest.mark.parametrize("mensaje", [
    "Estoy sin dolor, pero hinchado",
    "no me duele pero se me hinchó la cara",
    "no es nada grave, aunque se me rompió un diente",
    "No puedo esperar, tengo un dolor intenso",
    "No sé qué hacer, se me hinchó la cara",
    "No puedo dormir del dolor intenso",
])
def test_42_se_conserva_la_urgencia(mensaje):
    assert "urgencia_clinica" in evaluar(mensaje).senales


# --- 3) NFC y correos con espacios ----------------------------------------------------------------

def test_42_nombre_descompuesto_se_enmascara():
    compuesto, descompuesto = "María", "María"
    for texto, nombre in [(f"Hola soy {descompuesto}", "María López"),
                          (f"Hola soy {compuesto}", "María López"),
                          (f"Hola soy {descompuesto}", "María López")]:
        t = enmascarar(texto, nombre=nombre)
        assert t == "Hola soy [NOMBRE]", (texto, nombre, t)


@pytest.mark.parametrize("texto", [
    "mi correo es ana @ ejemplo . com gracias",
    "mi correo es ana@ejemplo .com gracias",
    "mi correo es ana @ejemplo.com gracias",
    "mi correo es ana.b+x@sub.ejemplo.com gracias",
    "mi correo es josé@ejemplo.ec gracias",
])
def test_42_correos_con_espacios(texto):
    t = enmascarar(texto)
    assert t == "mi correo es [CORREO] gracias", t


@pytest.mark.parametrize("texto", [
    "nos vemos @ las 5",
    "cuesta @ 20.000",
    "@clinica en Instagram",
    "escríbeme a la @ que te pasé",
    "promo 2x1 @ 15.50 usd",
])
def test_42_arroba_suelta_no_es_correo(texto):
    assert enmascarar(texto) == texto


# --- 4) Tope de longitud y rendimiento ----------------------------------------------------------------

MIB = 1024 * 1024
ADVERSARIOS = {
    "solo_digitos": "7" * MIB,
    "solo_espacios": " " * MIB,
    "sin_arroba": ("hola quisiera una cita para limpieza " * 40000)[:MIB],
    "muchas_arrobas": "a@" * (MIB // 2),
    "frase_repetida": ("Tengo dolor de muela. " * 50000)[:MIB],
    "palabra_larga": "a" * MIB,
    "digitos_y_espacios": "1 " * (MIB // 2),
    "solo_emojis": "😁" * (MIB // 4),
    "arrobas_y_puntos": "a@b." * (MIB // 4),
    # Hallazgo del 4.2 (no estaba en la lista): enlaces sin protocolo con muchas etiquetas.
    "puntos_alternados": "a." * (MIB // 2),
    "guiones_alternados": "a-" * (MIB // 2),
    "digitos_y_puntos": "7." * (MIB // 2),
}


@pytest.mark.parametrize("nombre", list(ADVERSARIOS))
def test_42_entradas_adversarias_de_1_mib(nombre):
    texto = ADVERSARIOS[nombre]
    inicio = time.perf_counter()
    evaluar(texto, servicio="Limpieza")
    contenido_para_llm(texto, servicio="Limpieza", nombre="Carla Mendoza")
    transcurrido = time.perf_counter() - inicio
    assert transcurrido < 1.0, f"{nombre}: {transcurrido:.2f} s"


def test_42_tope_conserva_inicio_y_final_para_las_reglas():
    relleno = "bla " * 10000  # unos 40 000 caracteres
    assert len(relleno) > MAX_TEXTO_PROCESADO
    assert "pide_agendar_o_llamada" in evaluar("Hola. " + relleno + " Quiero agendar una cita.").senales
    assert "urgencia_clinica" in evaluar("Tengo un dolor intenso. " + relleno).senales


def test_42_la_lista_negada_no_anula_la_peticion_de_cita():
    s = evaluar("No tengo dolor, fiebre y quiero una cita").senales
    assert "pide_agendar_o_llamada" in s and "urgencia_clinica" not in s
