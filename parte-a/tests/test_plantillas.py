"""Bloque 5a: plantillas sin LLM (aviso caliente, seguimiento tibio, novedad de duplicado)."""
import re

import pytest

from app.agenda import ZONA, proponer_citas
from app.plantillas import aviso_caliente, novedad_duplicado, seguimiento_tibio
from datetime import datetime

AHORA = datetime(2026, 11, 2, 8, 0, tzinfo=ZONA)
MENSAJE_LIBRE = "Tengo un secreto: mi vecino Pedro me debe plata"
MARKDOWN = re.compile(r"[*`#\[\]|<>]|__")


def datos_aviso(**cambios):
    d = {"nombre": "Carla Mendoza", "telefono_e164": "+593991234567", "email": "carla@example.com",
         "servicio": "Blanqueamiento", "ruta": "caliente", "motivo": "Caliente por reglas: pide agendar o que lo llamen",
         "estado_enriquecimiento": "paciente", "tratamientos_pendientes": 2, "event_id": "evt_01",
         "lead_id": "lead-123", "received_at": "2026-11-02T13:00:00.000000Z",
         "citas": proponer_citas("Blanqueamiento", None, AHORA, ()), "mensaje": MENSAJE_LIBRE}
    d.update(cambios)
    return d


def test_aviso_caliente_contenido():
    t = aviso_caliente(datos_aviso())
    for esperado in ("Carla Mendoza", "+593991234567", "Blanqueamiento", "caliente", "pide agendar",
                     "con tratamiento pendiente", "Valoración inicial", "evt_01", "lead-123",
                     "2026-11-02T13:00:00", "tentativa"):
        assert esperado in t, esperado
    assert MENSAJE_LIBRE not in t and "Pedro" not in t
    assert not MARKDOWN.search(t.replace("evt_01", ""))


def test_aviso_contacto_principal_y_estado_de_paciente():
    t = aviso_caliente(datos_aviso(telefono_e164=None))
    assert "carla@example.com" in t
    assert "Paciente: no" in aviso_caliente(datos_aviso(estado_enriquecimiento="no_paciente"))
    assert "ambigua" in aviso_caliente(datos_aviso(estado_enriquecimiento="ambigua"))
    assert "sin verificar" in aviso_caliente(datos_aviso(estado_enriquecimiento="no_configurado"))
    t = aviso_caliente(datos_aviso(tratamientos_pendientes=0))
    assert "Paciente: sí" in t and "pendiente" not in t.split("Paciente:")[1].split("\n")[0]


def test_aviso_sin_citas_lo_dice():
    assert "sin propuesta automática" in aviso_caliente(datos_aviso(citas=[]))


@pytest.mark.parametrize("servicio", ["Limpieza", "Valoración", "Otro", "Blanqueamiento",
                                       "Combo limpieza + blanqueamiento", "Ortodoncia", "Alineadores", None])
@pytest.mark.parametrize("url", [None, "https://agenda.example/cerro-alto"])
def test_seguimiento_tibio(servicio, url):
    citas = proponer_citas(servicio, None, AHORA, ())
    t = seguimiento_tibio("carla mendoza", servicio, citas, url)
    assert len(t) < 500
    assert t.startswith("Hola Carla")
    assert not MARKDOWN.search(t) and "\n#" not in t
    assert "usted" not in t.lower()
    assert "sede" in t.lower()
    for prohibido in ("descuento", "%", "garantiz", "resultado", "te recomiendo", "deberías"):
        assert prohibido not in t.lower(), prohibido
    if servicio != "Ortodoncia" and servicio != "Alineadores":
        assert "promoción" not in t.lower() and "sin costo" not in t.lower()
    if url:
        assert url in t and f"]({url})" not in t
    else:
        assert "http" not in t
    assert MENSAJE_LIBRE not in t


def test_seguimiento_solo_hechos_de_la_ficha():
    citas = proponer_citas("Limpieza", None, AHORA, ())
    t = seguimiento_tibio("Carla", "Limpieza", citas, None)
    assert "USD 45" in t and "limpieza dental" in t and "60 minutos" in t
    precios = set(re.findall(r"USD (\d+)", t))
    assert precios <= {"45", "25"}


def test_seguimiento_ortodoncia_menciona_p2():
    t = seguimiento_tibio("Ana", "Ortodoncia", proponer_citas("Ortodoncia", None, AHORA, ()), None)
    assert "sin costo de lunes a viernes" in t and "USD 25" in t


def test_seguimiento_sin_nombre_y_sin_citas():
    t = seguimiento_tibio("", "Limpieza", [], None)
    assert t.startswith("Hola,") and "qué día" in t.lower()
    t = seguimiento_tibio(None, None, [], None)
    assert t.startswith("Hola,") and len(t) < 500


def test_seguimiento_nombre_largo_no_rompe_el_limite():
    t = seguimiento_tibio("A" * 300, "Combo limpieza + blanqueamiento",
                          proponer_citas("Combo limpieza + blanqueamiento", None, AHORA, ()),
                          "https://agenda.example/" + "x" * 100)
    assert len(t) < 500


def test_novedad_duplicado():
    t = novedad_duplicado({"nombre": "Carla Mendoza", "telefono_e164": "+593991234567", "email": None,
                           "senales": ["urgencia_clinica"], "duplicado_de": "evt_01", "event_id": "evt_03",
                           "received_at": "2026-11-02T13:00:00.000000Z", "lead_id": "lead-123",
                           "mensaje": MENSAJE_LIBRE})
    assert "Actualización de un lead existente" in t and "urgencia clínica" in t
    assert "evt_01" in t and "evt_03" in t and MENSAJE_LIBRE not in t
