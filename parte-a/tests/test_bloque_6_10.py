"""Bloque 6.10: forma_inesperada de la API de pacientes se reprograma antes de degradar, y
el verificador de payloads acepta tibio o caliente para el evento 06."""
import json
import logging
from datetime import timedelta

import httpx
import pytest

from app import enriquecimiento
from app.errores import MAX_REPROGRAMACIONES
from tests.test_pacientes import (  # noqa: F401  (fixtures y helpers del Bloque 3)
    EMAIL, TEL, TOKEN, ApiFalsa, ahora, cliente, con, contexto, enr, lead, paciente, paginar, procesar,
    r429, recibir, ruta_db, trabajo_de, tratamiento,
)
from tests.test_verificar_payloads import cargar_script, esperado

FORMA = {"data": [{"id": "pac_1"}], "next_cursor": None}  # falta nombre_completo


def forma():
    return httpx.Response(200, json=FORMA)


def ruta(con, i):
    return con.execute("SELECT estado, ruta FROM eventos WHERE id = ?", (i,)).fetchone()


def incidencias(con, codigo):
    return con.execute("SELECT * FROM incidencias WHERE codigo = ?", (codigo,)).fetchall()


def procesar_hasta(ruta_db, ctx, veces, paso_s=60):
    momento = ahora()
    for _ in range(veces):
        procesar(ruta_db, ctx, momento)
        momento += timedelta(seconds=paso_s)


# --- 1) forma_inesperada como fallo transitorio --------------------------------------------------

def test_a_forma_inesperada_en_busqueda_y_luego_valida(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1")], {"pac_1": [tratamiento("t1")]}, respuestas=[forma()])
    ctx = contexto(api)
    i = recibir(cliente, lead())
    procesar(ruta_db, ctx, ahora())
    t = trabajo_de(con, i)
    assert (t["estado"], t["intentos"], t["reprogramaciones"]) == ("pendiente", 0, 1)
    assert enr(con, i) is None  # aún sin enriquecer: no degradó
    procesar(ruta_db, ctx, ahora() + timedelta(seconds=60))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "paciente" and e["tratamientos_pendientes"] == 1
    assert tuple(ruta(con, i)) == ("procesado", "caliente")
    assert len(api.peticiones) == 3  # búsqueda inválida; búsqueda y tratamientos válidos
    assert incidencias(con, "forma_inesperada") == []


def test_b_forma_inesperada_en_tratamientos_y_luego_valida(cliente, con, ruta_db):
    busqueda = httpx.Response(200, json=paginar([paciente("pac_1")], None))
    api = ApiFalsa([paciente("pac_1")], {"pac_1": [tratamiento("t1")]},
                   respuestas=[busqueda, httpx.Response(200, json={"data": [{"tipo": ""}], "next_cursor": None})])
    ctx = contexto(api)
    i = recibir(cliente, lead())
    procesar(ruta_db, ctx, ahora())
    assert trabajo_de(con, i)["reprogramaciones"] == 1 and enr(con, i) is None
    procesar(ruta_db, ctx, ahora() + timedelta(seconds=60))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "paciente" and e["tratamientos_pendientes"] == 1
    assert tuple(ruta(con, i)) == ("procesado", "caliente")
    assert api.rutas() == ["/v1/pacientes", "/v1/pacientes/pac_1/tratamientos"] * 2


def test_c_siempre_forma_inesperada_degrada_tras_agotar(cliente, con, ruta_db, caplog):
    caplog.set_level(logging.DEBUG)
    api = ApiFalsa([], respuestas=[forma() for _ in range(10)])
    ctx = contexto(api)
    i = recibir(cliente, lead())
    procesar_hasta(ruta_db, ctx, MAX_REPROGRAMACIONES + 2)
    assert len(api.peticiones) == MAX_REPROGRAMACIONES + 1  # 1 + 3 reprogramaciones, sin más
    t = trabajo_de(con, i)
    assert (t["estado"], t["intentos"], t["reprogramaciones"]) == ("hecho", 1, MAX_REPROGRAMACIONES)
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_disponible"
    assert e["motivo"] == enriquecimiento.MOTIVOS["forma_inesperada"]
    assert ruta(con, i)["estado"] == "procesado" and ruta(con, i)["ruta"] is not None  # sigue con reglas
    inc = incidencias(con, "forma_inesperada")
    assert len(inc) == 1 and inc[0]["ocurrencias"] == 1  # una sola, al agotar
    assert caplog.text.count("codigo=forma_inesperada") == 1
    # (e) el log no incluye datos personales ni el token
    texto = caplog.text + "".join(str(r.args) for r in caplog.records)
    for dato in (TOKEN, TEL, "0991234567", EMAIL, "Carla", "Mendoza", "pac_1"):
        assert dato not in texto, dato
    assert "reprogramado" in texto


def test_c_reprogramaciones_no_se_saltan_el_limitador(cliente, con, ruta_db):
    """Cada vuelta es una sesión nueva con su presupuesto; el limitador es el del cliente."""
    api = ApiFalsa([], respuestas=[forma() for _ in range(10)])
    ctx = contexto(api, limite=1)  # 1 petición por minuto
    i = recibir(cliente, lead())
    procesar_hasta(ruta_db, ctx, MAX_REPROGRAMACIONES + 2, paso_s=61)  # el reloj del limitador no avanza
    assert len(api.peticiones) == 1  # las vueltas siguientes chocan con el limitador, no lo saltan
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_disponible"
    assert e["motivo"] == enriquecimiento.MOTIVOS["limite_de_solicitudes"]


# --- (d) 429 y 5xx no cambian -------------------------------------------------------------------

@pytest.mark.parametrize("fallo", [httpx.Response(503), httpx.Response(500), httpx.ReadTimeout("x")])
def test_d_5xx_y_timeout_no_se_reprograman(cliente, con, ruta_db, fallo):
    api = ApiFalsa([], respuestas=[fallo, fallo, fallo])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api), ahora())
    assert len(api.peticiones) == 2  # original + reintento corto, como antes
    t = trabajo_de(con, i)
    assert t["estado"] == "hecho" and t["reprogramaciones"] == 0
    assert enr(con, i)["estado_enriquecimiento"] == "no_disponible"


def test_d_429_sigue_reprogramando_con_su_retry_after(cliente, con, ruta_db):
    api = ApiFalsa([], respuestas=[r429("7")])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api), ahora())
    t = trabajo_de(con, i)
    assert (t["estado"], t["intentos"], t["reprogramaciones"]) == ("pendiente", 0, 1)


# --- 2) Verificador de payloads: rutas aceptadas por evento --------------------------------------

def test_esperado_06_acepta_tibio_o_caliente_y_el_resto_es_estricto():
    e = esperado()
    assert e["evento_06.json"]["rutas_aceptadas"] == ["tibio", "caliente"]
    assert e["evento_06.json"]["ruta"] == "tibio"  # sin la API de pacientes (los tests)
    assert e["evento_06.json"].get("nota")
    assert [n for n, v in e.items() if "rutas_aceptadas" in v] == ["evento_06.json"]


@pytest.mark.parametrize("ruta_real, ok", [("tibio", True), ("caliente", True), ("basura", False), (None, False)])
def test_verificador_06(ruta_real, ok):
    v = cargar_script()
    assert v.coincide(esperado()["evento_06.json"], "procesado", ruta_real) is ok


@pytest.mark.parametrize("nombre", ["evento_01.json", "evento_04.json", "evento_07.json", "evento_08.json",
                                    "evento_09.json", "evento_10.json"])
def test_verificador_resto_exige_su_ruta_exacta(nombre):
    v = cargar_script()
    esp = esperado()[nombre]
    assert v.coincide(esp, esp["estado"], esp["ruta"]) is True
    for otra in ("caliente", "tibio", "basura", None):
        if otra != esp["ruta"]:
            assert v.coincide(esp, esp["estado"], otra) is False, (nombre, otra)
    assert v.coincide(esp, "duplicado", esp["ruta"]) is False
