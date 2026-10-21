"""Bloque 3.2: correcciones del enriquecimiento tras la revisión del Bloque 3."""
import http.server
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import enriquecimiento
from app.pacientes import ClientePacientes
from app.trabajador import Contexto

from test_pacientes import (  # noqa: F401  (fixtures y helpers compartidos)
    API, TOKEN, ApiFalsa, cliente, con, contexto, enr, lead, paciente, procesar, r429, recibir,
    ruta_db, trabajo_de, tratamiento,
)
from app import config, db


def ahora():
    return datetime.now(timezone.utc) + timedelta(seconds=1)


# --- 1) Presupuesto total y tope de tamaño ---------------------------------------------------

class Goteo(http.server.BaseHTTPRequestHandler):
    """Responde 200 y envía el cuerpo de 1 byte cada 0,5 s."""
    def do_GET(self):
        cuerpo = b'{"data":[],"next_cursor":null}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        for b in cuerpo:
            try:
                self.wfile.write(bytes([b]))
                self.wfile.flush()
            except OSError:
                return
            time.sleep(0.5)

    def log_message(self, *args):
        pass


@pytest.fixture
def servidor_goteo():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Goteo)
    srv.daemon_threads = True
    srv.block_on_close = False
    hilo = threading.Thread(target=srv.serve_forever, daemon=True)
    hilo.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_32_presupuesto_total_con_servidor_que_gotea(cliente, con, ruta_db, servidor_goteo, monkeypatch):
    monkeypatch.setattr(enriquecimiento, "PRESUPUESTO_S", 2.0)
    cfg = config.cargar_config({"API_PACIENTES_URL": servidor_goteo, "API_PACIENTES_TOKEN": TOKEN})
    ctx = Contexto(cfg, ClientePacientes(servidor_goteo, TOKEN))
    i = recibir(cliente, lead())
    inicio = time.monotonic()
    try:
        procesar(ruta_db, ctx)
    finally:
        ctx.cerrar()
    transcurrido = time.monotonic() - inicio
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_disponible"
    assert transcurrido < 3.0, f"tardó {transcurrido:.1f} s"


def test_32_cuerpo_demasiado_grande_degrada(cliente, con, ruta_db):
    grande = b'{"data":[],"next_cursor":null' + b" " * (300 * 1024) + b"}"
    api = ApiFalsa([], respuestas=[httpx.Response(200, content=grande)])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    assert enr(con, i)["estado_enriquecimiento"] == "no_disponible"


# --- 2) Resultados parciales ------------------------------------------------------------------

def test_32_busqueda_incompleta_no_confirma_paciente(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1")] + [paciente(f"pac_x{k}", f"Otro {k}") for k in range(20)],
                   {"pac_1": [tratamiento("t1")]})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api, max_paginas=2))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "ambigua" and e["coincidencia"] == "ambigua"
    assert "incompleta" in e["motivo"].lower()
    assert e["paciente_id"] is None
    assert not [r for r in api.rutas() if r.endswith("/tratamientos")]


def test_32_tratamientos_incompletos_con_pendientes_es_un_minimo(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1")], {"pac_1": [tratamiento(f"t{k}") for k in range(20)]})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api, max_paginas=2))
    e = enr(con, i)
    assert e["tratamientos_pendientes"] == 6 and e["tratamientos_completos"] == 0


def test_32_tratamientos_incompletos_sin_pendientes_es_desconocido(cliente, con, ruta_db):
    trats = [tratamiento(f"t{k}", estado="completado") for k in range(6)] + [tratamiento("t9")]
    api = ApiFalsa([paciente("pac_1")], {"pac_1": trats})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api, max_paginas=2))
    e = enr(con, i)
    assert e["tratamientos_pendientes"] is None  # desconocido, nunca 0
    assert e["tratamientos_completos"] == 0


def test_32_tratamientos_completos(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1")], {"pac_1": [tratamiento("t1", estado="completado")]})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    assert e["tratamientos_pendientes"] == 0 and e["tratamientos_completos"] == 1


# --- 3) D7.2 en la etapa -----------------------------------------------------------------------

def test_32_padre_e_hijo_con_el_mismo_contacto_es_ambigua(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1", "Juan Pérez"), paciente("pac_2", "Juan Carlos Pérez")],
                   {"pac_1": [tratamiento("t1")], "pac_2": [tratamiento("t2")]})
    i = recibir(cliente, lead(nombre="Juan Pérez"))
    procesar(ruta_db, contexto(api))
    assert enr(con, i)["estado_enriquecimiento"] == "ambigua"
    assert not [r for r in api.rutas() if r.endswith("/tratamientos")]


# --- 4) Retry-After -----------------------------------------------------------------------------

def _proximo(con, i):
    return datetime.strptime(trabajo_de(con, i)["proximo_intento_at"],
                             db.FORMATO_UTC).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("cabecera, segundos", [("45", 45), ("120", 120), ("0", 1), (" 30 ", 30)])
def test_32_retry_after_hasta_120_reprograma_exacto(cliente, con, ruta_db, cabecera, segundos):
    api = ApiFalsa([], respuestas=[r429(cabecera)])
    i = recibir(cliente, lead())
    t0 = ahora()
    procesar(ruta_db, contexto(api), t0)
    t = trabajo_de(con, i)
    assert (t["estado"], t["reprogramaciones"], t["intentos"]) == ("pendiente", 1, 0)
    assert _proximo(con, i) == t0 + timedelta(seconds=segundos)


@pytest.mark.parametrize("cabecera", ["121", "500", "9" * 5000, "abc", "-5", "3.5", "+30", None])
def test_32_retry_after_mayor_o_invalido_degrada_sin_reprogramar(cliente, con, ruta_db, caplog, cabecera):
    resp = (r429(cabecera) if cabecera is not None
            else httpx.Response(429, json={"error": {"code": "rate_limited", "message": "x"}}))
    api = ApiFalsa([], respuestas=[resp])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api), ahora())
    t = trabajo_de(con, i)
    assert t["estado"] == "hecho" and t["reprogramaciones"] == 0
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_disponible"
    assert "límite de uso" in e["motivo"].lower()
    assert len(api.peticiones) == 1
    assert "limite_de_uso" in caplog.text  # queda registro


# --- 5) next_cursor ausente ---------------------------------------------------------------------

def test_32_sin_next_cursor_con_pagina_corta_advierte(cliente, con, ruta_db):
    pagina = {"data": [paciente("pac_1")]}
    api = ApiFalsa([], {"pac_1": []}, respuestas=[httpx.Response(200, json=pagina)])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "paciente"
    assert "next_cursor_ausente" in json.loads(e["advertencias"])


def test_32_sin_next_cursor_con_pagina_llena_es_forma_inesperada(cliente, con, ruta_db):
    pagina = {"data": [paciente("pac_1"), paciente("pac_2", "Otro"), paciente("pac_3", "Otro B")]}
    api = ApiFalsa([], respuestas=[httpx.Response(200, json=pagina)])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    assert enr(con, i)["estado_enriquecimiento"] == "no_disponible"


# --- 6) JSON anidado ------------------------------------------------------------------------------

def test_32_json_anidado_degrada_sin_gastar_intentos(cliente, con, ruta_db):
    anidado = b"[" * 50_000 + b"]" * 50_000
    api = ApiFalsa([], respuestas=[httpx.Response(200, content=anidado)])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    t = trabajo_de(con, i)
    assert t["estado"] == "hecho" and t["ultimo_error"] is None
    assert enr(con, i)["estado_enriquecimiento"] == "no_disponible"


# --- 7) Validación de lo que se guarda --------------------------------------------------------------

def test_32_ultima_visita_con_datos_personales_no_se_guarda(cliente, con, ruta_db, caplog):
    caplog.set_level(logging.DEBUG)
    basura = "Rosa Quishpe, teléfono 0987650021"
    api = ApiFalsa([paciente("pac_1", ultima_visita=basura)], {"pac_1": []})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "paciente" and e["ultima_visita"] is None
    assert "ultima_visita_invalida" in json.loads(e["advertencias"])
    volcado = "\n".join(con.iterdump())
    assert "Rosa Quishpe" not in volcado and "0987650021" not in volcado
    assert "Rosa Quishpe" not in caplog.text and "0987650021" not in caplog.text


@pytest.mark.parametrize("campo, valor, advertencia", [
    ("ultima_visita", "2026-02-30", "ultima_visita_invalida"),
    ("ultima_visita", "2026-3-1", "ultima_visita_invalida"),
    ("sede_habitual", "sur", "sede_invalida"),
    ("estado", "vip", "estado_paciente_invalido"),
])
def test_32_campo_invalido_queda_null_con_advertencia(cliente, con, ruta_db, campo, valor, advertencia):
    api = ApiFalsa([paciente("pac_1", **{campo: valor})], {"pac_1": []})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    columna = {"estado": "estado_paciente"}.get(campo, campo)
    assert e["estado_enriquecimiento"] == "paciente" and e[columna] is None
    assert advertencia in json.loads(e["advertencias"])


def test_32_paciente_id_invalido_no_se_guarda(cliente, con, ruta_db):
    api = ApiFalsa([paciente("p-1; DROP TABLE")], {})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_disponible" and e["paciente_id"] is None
    assert "DROP" not in "\n".join(con.iterdump())


def test_32_tipos_de_tratamiento_normalizados_y_validados(cliente, con, ruta_db):
    trats = [tratamiento("t1", "Ortodoncia"), tratamiento("t2", "Extracción muela"),
             tratamiento("t3", "Rosa Quishpe 0987650021 <script>"), tratamiento("t4", "x" * 41)]
    api = ApiFalsa([paciente("pac_1")], {"pac_1": trats})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    assert e["tratamientos_pendientes"] == 4  # se cuentan todos los pendientes
    assert json.loads(e["tipos_pendientes"]) == ["ortodoncia", "extraccion muela"]
    assert "tipo_tratamiento_invalido" in json.loads(e["advertencias"])
    assert "0987650021" not in "\n".join(con.iterdump())


# --- 3.2 (ajuste): paciente_id seguro en la URL, sin asumir prefijo ---------------------------

@pytest.mark.parametrize("pid", ["pac_8F2K1", "P-123", "a" * 64, "123", "ID_x-9"])
def test_32_paciente_id_valido(cliente, con, ruta_db, pid):
    api = ApiFalsa([paciente(pid)], {pid: [tratamiento("t1")]})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "paciente" and e["paciente_id"] == pid
    assert api.rutas()[-1] == f"/v1/pacientes/{pid}/tratamientos"


@pytest.mark.parametrize("pid", ["pac/1", "pac?1", "pac 1", "../x", "a" * 65, "",
                                 "pac%2F1", "pac#1", "pac\n1", "pac_1\n", "pác_1", "pac.1"])
def test_32_paciente_id_inseguro_degrada(cliente, con, ruta_db, pid):
    api = ApiFalsa([paciente(pid)], {})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_disponible" and e["paciente_id"] is None
    assert api.rutas() == ["/v1/pacientes"]  # nunca se construye la URL de tratamientos
