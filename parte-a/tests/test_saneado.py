"""Paso 0 (5b, D17): saneado de los campos del lead que entran en avisos y seguimientos."""
from datetime import datetime

import pytest

from app.agenda import ZONA, proponer_citas
from app.plantillas import aviso_caliente, limpiar_campo, novedad_duplicado, seguimiento_tibio

AHORA = datetime(2026, 11, 2, 8, 0, tzinfo=ZONA)
MALICIOSOS = [
    "Carla\nRuta: basura",
    "Carla\r\nMotivo: spam\rPaciente: no",
    "Carla ‮otset‬ ​Mendoza⁦x⁩",
    "C" * 5000,
    "Carla https://phishing.example/login www.otro.example",
    "*Carla* [a](http://x) _Mendoza_ `code`",
    "<b>Carla</b><script>alert(1)</script>",
    "Carla 😁🦷✨",
    "",
    "\t\t\n\n",
    "Carla\x00\x1b[31mRoja\x07",
]


@pytest.mark.parametrize("texto, maxlen, esperado", [
    ("Carla\nMendoza", 80, "Carla Mendoza"),
    ("  Carla \t  Mendoza  ", 80, "Carla Mendoza"),
    ("A‮B​C⁦D", 80, "ABCD"),
    ("María", 80, "María"),
    ("visita https://x.example/a y www.y.example", 80, "visita [enlace] y [enlace]"),
    ("abcdefghij", 5, "abcd…"),
    ("abc", 3, "abc"),
    ("", 10, ""),
    (None, 10, ""),
    ("x\x00y\x7fz", 10, "xyz"),            # controles: se eliminan
    ("<b>Carla</b> <i>M</i>", 80, "Carla M"),  # decisión extra: sin etiquetas HTML
    ("a\u2028b\x1cc\x85d", 10, "a b c d"),   # separadores de línea: espacio
])
def test_limpiar_campo(texto, maxlen, esperado):
    assert limpiar_campo(texto, maxlen) == esperado


def _datos(**cambios):
    d = {"nombre": "Carla Mendoza", "telefono_e164": "+593991234567", "email": "carla@example.com",
         "servicio": "Blanqueamiento", "ruta": "caliente", "motivo": "Caliente por reglas: pide agendar",
         "estado_enriquecimiento": "no_paciente", "tratamientos_pendientes": None, "event_id": "evt_01",
         "lead_id": "lead-1", "received_at": "2026-11-02T13:00:00.000000Z",
         "citas": proponer_citas("Blanqueamiento", None, AHORA, ())}
    d.update(cambios)
    return d


def _prefijos(texto):
    return [linea.split(":", 1)[0] for linea in texto.split("\n")]


BASE_AVISO = _prefijos(aviso_caliente(_datos()))
BASE_NOVEDAD = _prefijos(novedad_duplicado({**_datos(), "senales": ["urgencia_clinica"], "duplicado_de": "evt_00"}))


@pytest.mark.parametrize("campo", ["nombre", "telefono_e164", "email", "servicio", "event_id", "lead_id",
                                   "received_at", "motivo"])
@pytest.mark.parametrize("valor", MALICIOSOS)
def test_estructura_fija_del_aviso(campo, valor):
    t = aviso_caliente(_datos(**{campo: valor}))
    assert _prefijos(t) == BASE_AVISO, t
    assert "\r" not in t and "‮" not in t and "​" not in t and "\x1b" not in t
    assert "https://phishing" not in t and "<script>" not in t
    assert max(len(linea) for linea in t.split("\n")) < 1500


@pytest.mark.parametrize("campo", ["nombre", "telefono_e164", "email", "event_id", "duplicado_de", "lead_id"])
@pytest.mark.parametrize("valor", MALICIOSOS)
def test_estructura_fija_de_la_novedad(campo, valor):
    d = {**_datos(), "senales": ["urgencia_clinica"], "duplicado_de": "evt_00", campo: valor}
    t = novedad_duplicado(d)
    assert _prefijos(t) == BASE_NOVEDAD, t
    assert "\r" not in t and "‮" not in t


def test_nombre_con_salto_no_inyecta_una_linea_ruta():
    t = aviso_caliente(_datos(nombre="Carla\nRuta: basura"))
    lineas_ruta = [x for x in t.split("\n") if x.startswith("Ruta:")]
    assert lineas_ruta == ["Ruta: caliente. Motivo: Caliente por reglas: pide agendar"]


def test_limites_de_cada_campo():
    t = aviso_caliente(_datos(nombre="N" * 5000, servicio="S" * 5000, email="e" * 5000 + "@x.com"))
    linea = {x.split(":", 1)[0]: x.split(": ", 1)[1] for x in t.split("\n") if ": " in x}
    assert len(linea["Nombre"]) == 80 and linea["Nombre"].endswith("…")
    assert len(linea["Servicio de interés"]) == 60
    t = aviso_caliente(_datos(telefono_e164="+593 (99) 123-4567 <b>x</b>" + "9" * 100))
    contacto = [x for x in t.split("\n") if x.startswith("Contacto:")][0][len("Contacto: "):]
    assert set(contacto) <= set("0123456789+ ()-…") and len(contacto) <= 30
    t = aviso_caliente(_datos(telefono_e164=None, email="carla mendoza\n@example.com"))
    contacto = [x for x in t.split("\n") if x.startswith("Contacto:")][0][len("Contacto: "):]
    assert " " not in contacto and "\n" not in contacto


def test_servicio_del_catalogo_usa_el_valor_canonico():
    t = aviso_caliente(_datos(servicio="  ortodoncia "))
    assert "Servicio de interés: Ortodoncia" in t
    t = aviso_caliente(_datos(servicio="combo LIMPIEZA + blanqueamiento"))
    assert "Servicio de interés: Combo limpieza + blanqueamiento" in t


@pytest.mark.parametrize("nombre, saludo", [
    ("carla mendoza", "Hola Carla,"),
    ("O'Neil Pérez", "Hola O'neil,"),
    ("Ana-María López", "Hola Ana-maría,"),
    ("*Carla* Mendoza", "Hola Carla,"),
    ("<b>Ana</b> Pérez", "Hola Ana,"),
    ("😁 Carla", "Hola Carla,"),
    ("12345 67890", "Hola,"),
    ("Carla\nRuta: basura", "Hola Carla,"),
    ("https://x.example Carla", "Hola Carla,"),
    ("", "Hola,"),
    ("C" * 200, "Hola " + "C" + "c" * 29 + ","),
])
def test_nombre_de_pila_del_seguimiento(nombre, saludo):
    t = seguimiento_tibio(nombre, "Limpieza", proponer_citas("Limpieza", None, AHORA, ()), None)
    assert t.startswith(saludo + " "), t[:60]
    assert "\n" not in t and len(t) < 500


@pytest.mark.parametrize("valor", MALICIOSOS)
def test_seguimiento_con_servicio_malicioso(valor):
    t = seguimiento_tibio("Carla", valor, [], None)
    assert "\n" not in t and "https://phishing" not in t and "<script>" not in t and len(t) < 500
