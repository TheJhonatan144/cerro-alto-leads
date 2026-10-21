"""Bloque 3: etapa enriquecer_paciente contra una API de pacientes simulada (MockTransport).

Ningún test sale de la máquina: conftest bloquea cualquier conexión fuera de loopback.
"""
import json
import logging
import socket
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db, enriquecimiento, trabajador
from app.errores import MAX_REPROGRAMACIONES, Reprogramar
from app.main import create_app
from app.pacientes import ClientePacientes, Limitador
from app.trabajador import Contexto, procesar_pendientes, reclamar_trabajo, recuperar_expirados

from conftest import RedExternaBloqueada

PAYLOADS = Path(__file__).resolve().parents[2] / "payloads"
URL = "/webhook/leads"
TOKEN = "tok-SECRETO-no-debe-salir-123"
API = "https://api-pacientes.test"
TEL = "+593991234567"
EMAIL = "carla.mendoza@example.com"


# --- API falsa ------------------------------------------------------------------------

def paciente(id_, nombre="Carla Mendoza", **extra):
    base = {"id": id_, "nombre_completo": nombre, "telefono": TEL, "email": EMAIL,
            "fecha_nacimiento": "1990-05-01", "sede_habitual": "norte",
            "ultima_visita": "2026-03-10", "estado": "activo"}
    base.update(extra)
    return base


def tratamiento(id_, tipo="Limpieza", estado="pendiente"):
    return {"id": id_, "tipo": tipo, "pieza": None, "estado": estado, "fecha_plan": "2026-12-01"}


def paginar(elementos, cursor, tam=3):
    """Simula la paginación de la API: máximo 3 por página, cursor = índice."""
    inicio = int(cursor or 0)
    trozo = elementos[inicio:inicio + tam]
    siguiente = str(inicio + tam) if inicio + tam < len(elementos) else None
    return {"data": trozo, "next_cursor": siguiente}


class ApiFalsa:
    def __init__(self, pacientes=(), tratamientos=None, respuestas=None, reloj=None, demora_s=0.0):
        self.pacientes = list(pacientes)
        self.tratamientos = tratamientos or {}
        self.respuestas = list(respuestas or [])  # respuestas forzadas, en orden
        self.peticiones: list[httpx.Request] = []
        self.reloj = reloj
        self.demora_s = demora_s

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.peticiones.append(request)
        if self.reloj is not None and self.demora_s:
            limite = request.extensions["timeout"]["read"]
            if self.demora_s > limite:  # tarda más que el timeout de esta petición
                self.reloj.avanzar(limite)
                raise httpx.ReadTimeout("simulado", request=request)
            self.reloj.avanzar(self.demora_s)
        if self.respuestas:
            r = self.respuestas.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        cursor = request.url.params.get("cursor")
        partes = request.url.path.strip("/").split("/")
        if partes == ["v1", "pacientes"]:
            return httpx.Response(200, json=paginar(self.pacientes, cursor))
        if len(partes) == 4 and partes[3] == "tratamientos":
            if partes[2] not in self.tratamientos:
                return httpx.Response(404, json={"error": {"code": "not_found", "message": "x"}})
            return httpx.Response(200, json=paginar(self.tratamientos[partes[2]], cursor))
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "x"}})

    def rutas(self):
        return [r.url.path for r in self.peticiones]


class Reloj:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def avanzar(self, s):
        self.t += s


# --- Fixtures ---------------------------------------------------------------------------

@pytest.fixture
def ruta_db(tmp_path) -> Path:
    return tmp_path / "leads.db"


@pytest.fixture
def cliente(ruta_db):
    with TestClient(create_app(ruta_db)) as c:  # trabajador apagado
        yield c


@pytest.fixture
def con(ruta_db, cliente):
    c = db.conectar(ruta_db)
    yield c
    c.close()


def contexto(api, *, limite=25, max_paginas=5, reloj=None) -> Contexto:
    cfg = config.cargar_config({"API_PACIENTES_URL": API, "API_PACIENTES_TOKEN": TOKEN})
    cp = ClientePacientes(API, TOKEN, limite_por_min=limite, max_paginas=max_paginas,
                          transport=httpx.MockTransport(api), reloj=reloj or Reloj())
    return Contexto(cfg, cp)


def lead(nombre="Carla Mendoza", phone="0991234567", email=EMAIL, event_id=None):
    # event_id opaco, como los del conector: el webhook lo registra en el log.
    return json.dumps({"event_id": event_id or f"evt_{uuid.uuid4().hex}",
                       "event_type": "lead.created",
                       "lead": {"full_name": nombre, "phone": phone, "email": email,
                                "message": "Hola"}}).encode()


def recibir(cliente, cuerpo) -> int:
    r = cliente.post(URL, content=cuerpo)
    assert r.status_code in (200, 202)
    with sqlite3.connect(cliente.app.state.db_path) as c:
        return c.execute("SELECT id FROM eventos WHERE recepcion_id = ?",
                         (r.json()["recepcion_id"],)).fetchone()[0]


def ahora():
    return datetime.now(timezone.utc) + timedelta(seconds=1)


def enr(con, evento_id):
    return con.execute("SELECT * FROM enriquecimientos WHERE evento_id = ?", (evento_id,)).fetchone()


def trabajo_de(con, evento_id):
    return con.execute("SELECT * FROM trabajos WHERE evento_id = ?", (evento_id,)).fetchone()


def procesar(ruta_db, ctx, momento=None):
    return procesar_pendientes(ruta_db, momento or ahora(), ctx=ctx)


# Bloque 6.10: forma_inesperada se reprograma hasta MAX_REPROGRAMACIONES antes de degradar.
VUELTAS_FORMA = MAX_REPROGRAMACIONES + 1  # peticiones de búsqueda hasta degradar


def procesar_agotando(ruta_db, ctx, paso_s=60):
    """Procesa una vez y otra por cada reprogramación posible (avanzando el reloj)."""
    momento = ahora()
    for _ in range(VUELTAS_FORMA):
        procesar(ruta_db, ctx, momento)
        momento += timedelta(seconds=paso_s)


# --- Coincidencias (D7) ----------------------------------------------------------------

def test_paciente_unico_con_tratamientos_en_tres_paginas(cliente, con, ruta_db):
    trats = [tratamiento(f"t{i}", tipo, estado) for i, (tipo, estado) in enumerate([
        ("Ortodoncia", "pendiente"), ("Limpieza", "completado"), ("Endodoncia", "pendiente"),
        ("Corona", "pendiente"), ("Limpieza", "pendiente"), ("Ortodoncia", "pendiente"),
        ("Implante", "cancelado")])]
    api = ApiFalsa([paciente("pac_1", nombre="CARLA  MENDÓZA")], {"pac_1": trats})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "paciente" and e["coincidencia"] == "unica"
    assert e["paciente_id"] == "pac_1" and e["sede_habitual"] == "norte"
    assert e["ultima_visita"] == "2026-03-10" and e["estado_paciente"] == "activo"
    assert e["tratamientos_pendientes"] == 5
    # Normalizados (Bloque 3.2): minúsculas y sin tildes.
    assert json.loads(e["tipos_pendientes"]) == ["ortodoncia", "endodoncia", "corona", "limpieza"]
    assert e["tratamientos_completos"] == 1
    assert json.loads(e["advertencias"]) == []
    assert api.rutas().count("/v1/pacientes/pac_1/tratamientos") == 3  # 3 + 3 + 1
    assert trabajo_de(con, i)["estado"] == "hecho"


def test_no_se_guardan_datos_personales_del_paciente(con, cliente):
    columnas = {r[1] for r in con.execute("PRAGMA table_info(enriquecimientos)")}
    assert not columnas & {"nombre", "nombre_completo", "telefono", "email", "fecha_nacimiento"}


def test_sin_coincidencias(cliente, con, ruta_db):
    api = ApiFalsa([])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_paciente" and e["coincidencia"] == "ninguna"
    assert api.rutas() == ["/v1/pacientes"]


def test_varios_sin_coincidencia_de_nombre_es_ambigua(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1", "Rosa Quishpe"), paciente("pac_2", "Luis Mendoza")],
                   {"pac_1": [tratamiento("t1")], "pac_2": [tratamiento("t2")]})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "ambigua" and e["coincidencia"] == "ambigua"
    assert e["paciente_id"] is None and e["tratamientos_pendientes"] is None
    assert e["motivo"]
    assert not any("tratamientos" in r for r in api.rutas())


def test_un_solo_candidato_con_otro_nombre_es_ambigua(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1", "Rosa Quishpe")], {"pac_1": [tratamiento("t1")]})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    assert enr(con, i)["estado_enriquecimiento"] == "ambigua"
    assert not any("tratamientos" in r for r in api.rutas())


def test_dos_con_el_mismo_nombre_es_ambigua(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1"), paciente("pac_2", "carla mendoza")],
                   {"pac_1": [tratamiento("t1")], "pac_2": [tratamiento("t2")]})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    assert enr(con, i)["estado_enriquecimiento"] == "ambigua"
    assert not any("tratamientos" in r for r in api.rutas())


def test_mismo_paciente_repetido_en_paginas_cuenta_una_vez(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_9", "Otro"), paciente("pac_8", "Otro B"), paciente("pac_1"),
                    paciente("pac_1")], {"pac_1": []})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    assert enr(con, i)["estado_enriquecimiento"] == "paciente"
    assert enr(con, i)["tratamientos_pendientes"] == 0


# --- Paginación y codificación -----------------------------------------------------------

def test_paginacion_usa_el_cursor(cliente, con, ruta_db):
    api = ApiFalsa([paciente(f"pac_x{k}", f"Otro {k}") for k in range(5)] + [paciente("pac_1")], {"pac_1": []})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    busquedas = [r for r in api.peticiones if r.url.path == "/v1/pacientes"]
    assert [r.url.params.get("cursor") for r in busquedas] == [None, "3"]
    assert enr(con, i)["estado_enriquecimiento"] == "paciente"


def test_tope_de_paginas_con_advertencia(cliente, con, ruta_db):
    api = ApiFalsa([paciente(f"pac_x{k}", f"Otro {k}") for k in range(30)])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api, max_paginas=5))
    assert api.rutas().count("/v1/pacientes") == 5
    assert "tope_paginas_pacientes" in json.loads(enr(con, i)["advertencias"])


def test_tope_de_paginas_de_tratamientos(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1")], {"pac_1": [tratamiento(f"t{k}") for k in range(20)]})
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api, max_paginas=2))
    e = enr(con, i)
    assert "tope_paginas_tratamientos" in json.loads(e["advertencias"])
    assert e["tratamientos_pendientes"] == 6


def test_mas_del_telefono_codificado_y_una_sola_consulta_con_ambos(cliente, con, ruta_db):
    api = ApiFalsa([])
    recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    [r] = api.peticiones
    assert b"telefono=%2B593991234567" in r.url.query
    assert b"email=carla.mendoza%40example.com" in r.url.query
    assert parse_qs(urlsplit(str(r.url)).query)["telefono"] == [TEL]


def test_solo_correo_si_no_hay_telefono(cliente, con, ruta_db):
    api = ApiFalsa([])
    recibir(cliente, lead(phone=None))
    procesar(ruta_db, contexto(api))
    assert "telefono" not in api.peticiones[0].url.params
    assert api.peticiones[0].url.params["email"] == EMAIL


# --- 429 y limitador (D6) -----------------------------------------------------------------

def r429(retry_after="7"):
    return httpx.Response(429, headers={"Retry-After": retry_after},
                          json={"error": {"code": "rate_limited", "message": "x"}})


def test_429_reprograma_sin_gastar_intento_y_tras_tres_degrada(cliente, con, ruta_db):
    api = ApiFalsa([], respuestas=[r429(), r429(), r429(), r429()])
    ctx = contexto(api)
    i = recibir(cliente, lead())
    t0 = ahora()
    procesar(ruta_db, ctx, t0)
    t = trabajo_de(con, i)
    assert (t["estado"], t["intentos"], t["reprogramaciones"]) == ("pendiente", 0, 1)
    assert t["lease_hasta"] is None and t["etapa"] == "normalizar_y_duplicados"
    assert datetime.strptime(t["proximo_intento_at"], db.FORMATO_UTC).replace(
        tzinfo=timezone.utc) == t0 + timedelta(seconds=7)
    momento = t0
    for n in (2, 3):
        momento += timedelta(seconds=7)
        procesar(ruta_db, ctx, momento)
        assert trabajo_de(con, i)["reprogramaciones"] == n and trabajo_de(con, i)["intentos"] == 0
    procesar(ruta_db, ctx, momento + timedelta(seconds=7))
    t = trabajo_de(con, i)
    assert t["estado"] == "hecho" and t["intentos"] == 1
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_disponible"
    assert e["motivo"] == enriquecimiento.MOTIVOS["limite_de_solicitudes"]
    assert len(api.peticiones) == 4


# test_retry_after_con_tope_y_por_defecto se eliminó en el Bloque 3.2: la regla cambió
# (ver test_pacientes_3_2.py, test_32_retry_after_*).


def test_limitador_25_por_minuto():
    reloj = Reloj()
    lim = Limitador(25, reloj)
    for _ in range(25):
        lim.tomar()
        reloj.avanzar(1)  # 25 en 25 s
    with pytest.raises(Reprogramar) as exc:
        lim.tomar()
    assert exc.value.segundos == 35  # la primera marca sale de la ventana a los 60 s
    reloj.avanzar(35)
    lim.tomar()  # ya hay cupo


def test_limitador_sin_cupo_reprograma_el_trabajo_sin_dormir(cliente, con, ruta_db):
    reloj = Reloj()
    api = ApiFalsa([paciente("pac_1")], {"pac_1": []})
    ctx = contexto(api, limite=1, reloj=reloj)  # 1 por minuto: la de tratamientos no cabe
    i = recibir(cliente, lead())
    t0 = ahora()
    procesar(ruta_db, ctx, t0)
    t = trabajo_de(con, i)
    assert (t["estado"], t["intentos"], t["reprogramaciones"]) == ("pendiente", 0, 1)
    assert len(api.peticiones) == 1  # no esperó: la segunda petición no se hizo
    assert reloj() == 1000.0  # nadie durmió
    reloj.avanzar(60)
    procesar(ruta_db, ctx, t0 + timedelta(seconds=60))
    # Al volver, el cupo es otra vez de 1: la búsqueda pasa y los tratamientos no.
    assert trabajo_de(con, i)["reprogramaciones"] == 2


def test_reprogramar_generico_agotado_cuenta_como_fallo(cliente, con, ruta_db, monkeypatch):
    def siempre_reprograma(*_):
        raise Reprogramar(5)

    monkeypatch.setattr(trabajador, "ETAPAS", [trabajador.Etapa("x", siempre_reprograma)])
    i = recibir(cliente, lead())
    momento = ahora()
    for _ in range(4):
        procesar(ruta_db, contexto(ApiFalsa()), momento)
        momento += timedelta(seconds=5)
    t = trabajo_de(con, i)
    assert t["reprogramaciones"] == 3 and t["intentos"] == 1
    assert t["ultimo_error"] == "ReprogramacionesAgotadas" and t["estado"] == "pendiente"


# --- Fallos de la API (D5) -----------------------------------------------------------------

@pytest.mark.parametrize("fallo, codigo", [
    (httpx.ReadTimeout("x"), "timeout"),
    (httpx.ConnectError("x"), "error_de_red"),
    (httpx.Response(503, json={"error": {"code": "unavailable", "message": "x"}}), "error_del_servidor"),
])
def test_transitorio_un_reintento_y_luego_no_disponible(cliente, con, ruta_db, fallo, codigo):
    api = ApiFalsa([paciente("pac_1")], respuestas=[fallo, fallo, fallo])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    assert len(api.peticiones) == 2  # original + un reintento
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_disponible"
    assert e["motivo"] == enriquecimiento.MOTIVOS[codigo]
    assert trabajo_de(con, i)["estado"] == "hecho"  # degradar no es fallar el trabajo


def test_transitorio_que_se_recupera_en_el_reintento(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1")], {"pac_1": []}, respuestas=[httpx.Response(502)])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    assert enr(con, i)["estado_enriquecimiento"] == "paciente"


def test_401_sin_reintento_y_log_error(cliente, con, ruta_db, caplog):
    api = ApiFalsa([], respuestas=[httpx.Response(401, json={"error": {"code": "unauthorized",
                                                                       "message": "x"}})] * 2)
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    assert len(api.peticiones) == 1
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_disponible" and "credenciales" in e["motivo"]
    assert any(r.levelno == logging.ERROR and "credenciales" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("respuesta", [
    httpx.Response(400, json={"error": {"code": "bad_request", "message": "x"}}),
    httpx.Response(302, headers={"Location": "https://otro.test/"}),
])
def test_400_o_redireccion_degradan(cliente, con, ruta_db, respuesta):
    api = ApiFalsa([], respuestas=[respuesta])
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    assert len(api.peticiones) == 1  # no reintenta ni sigue redirecciones
    assert enr(con, i)["estado_enriquecimiento"] == "no_disponible"
    assert trabajo_de(con, i)["estado"] == "hecho"


# Bloque 6.10 (cambio de regla): antes degradaban en la primera vuelta; ahora se reprograman
# hasta MAX_REPROGRAMACIONES y después degradan igual.
@pytest.mark.parametrize("respuesta", [
    httpx.Response(200, content=b"<html>no es json</html>"),
    httpx.Response(200, json={"data": "no es lista", "next_cursor": None}),
    httpx.Response(200, json={"data": [{"id": "pac_1"}], "next_cursor": None}),
    httpx.Response(200, json={"data": [], "next_cursor": 5}),
    httpx.Response(200, json=[1, 2, 3]),
])
def test_json_invalido_o_forma_inesperada_degradan_tras_reprogramar(cliente, con, ruta_db, respuesta):
    api = ApiFalsa([], respuestas=[respuesta] * VUELTAS_FORMA)
    i = recibir(cliente, lead())
    procesar_agotando(ruta_db, contexto(api))
    assert len(api.peticiones) == VUELTAS_FORMA  # una por vuelta: sin reintento corto ni redirecciones
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_disponible"
    assert e["motivo"] == enriquecimiento.MOTIVOS["forma_inesperada"]
    t = trabajo_de(con, i)
    assert (t["estado"], t["reprogramaciones"]) == ("hecho", MAX_REPROGRAMACIONES)


def test_404_en_tratamientos(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1")], {})  # p1 sin tratamientos -> 404
    i = recibir(cliente, lead())
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "paciente" and e["paciente_id"] == "pac_1"
    assert e["tratamientos_pendientes"] is None
    assert "tratamientos_no_disponibles" in json.loads(e["advertencias"])


# --- Sin datos personales en los logs ------------------------------------------------------

def test_token_telefono_y_correo_fuera_de_todos_los_logs(cliente, con, ruta_db, caplog):
    caplog.set_level(logging.DEBUG)  # raíz en DEBUG: httpx/httpcore deben seguir callados
    api = ApiFalsa([paciente("pac_1")], {"pac_1": [tratamiento("t1")]},
                   respuestas=[httpx.Response(503), httpx.Response(200, json={"data": [paciente("pac_1")],
                                                                              "next_cursor": None})])
    recibir(cliente, lead())
    recibir(cliente, lead(nombre="Otra", phone="0987654321", email="otra@example.com"))
    ctx = contexto(api)
    procesar(ruta_db, ctx)
    api.respuestas = [httpx.Response(401)]
    recibir(cliente, lead(nombre="Tercera", phone="0998877665", email="tercera@example.com"))
    procesar(ruta_db, ctx)
    assert len(api.peticiones) >= 4
    texto = caplog.text + "".join(str(r.args) for r in caplog.records)
    for secreto in (TOKEN, TEL, "%2B593991234567", "0991234567", EMAIL, "carla.mendoza%40example.com",
                    "+593987654321", "otra@example.com", "tercera@example.com", "Carla Mendoza"):
        assert secreto not in texto, secreto
    assert not [r for r in caplog.records if r.name.startswith(("httpx", "httpcore"))]
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    assert TOKEN not in repr(ctx.cfg) and TOKEN not in repr(ctx.pacientes)


# --- Estados ignorados, idempotencia, reanudación, presupuesto ---------------------------

def test_rechazados_y_duplicados_se_ignoran(cliente, con, ruta_db):
    api = ApiFalsa([])
    i1 = recibir(cliente, (PAYLOADS / "evento_01.json").read_bytes())
    i3 = recibir(cliente, (PAYLOADS / "evento_03.json").read_bytes())  # duplicado de 01
    ir = recibir(cliente, lead(phone="1111111111", email=None))  # sin contacto válido
    procesar(ruta_db, contexto(api))
    with sqlite3.connect(ruta_db) as c:
        estados = dict(c.execute("SELECT id, estado FROM eventos").fetchall())
    assert estados[i3] == "duplicado" and estados[ir] == "rechazado"
    assert enr(con, i1) is not None
    assert enr(con, i3) is None and enr(con, ir) is None
    assert len(api.peticiones) == 1  # solo por el 01
    assert trabajo_de(con, i3)["estado"] == "hecho" and trabajo_de(con, ir)["estado"] == "hecho"


def test_repetir_la_etapa_no_duplica_filas(cliente, con, ruta_db):
    api = ApiFalsa([paciente("pac_1")], {"pac_1": [tratamiento("t1")]})
    ctx = contexto(api)
    i = recibir(cliente, lead())
    procesar(ruta_db, ctx)
    t = trabajo_de(con, i)
    enriquecimiento.enriquecer_paciente(con, t, ctx)
    enriquecimiento.enriquecer_paciente(con, t, ctx)
    assert con.execute("SELECT COUNT(*) FROM enriquecimientos").fetchone()[0] == 1
    assert enr(con, i)["estado_enriquecimiento"] == "paciente"


def test_reanudacion_tras_reinicio(cliente, con, ruta_db, monkeypatch):
    """El proceso murió tras normalizar: al volver se recupera por lease y solo enriquece."""
    i = recibir(cliente, lead())
    t0 = ahora()
    t = reclamar_trabajo(con, t0, lease_s=60)
    trabajador._normalizar_y_duplicados(con, t, contexto(ApiFalsa()))
    con.execute("UPDATE trabajos SET etapa = 'normalizar_y_duplicados' WHERE id = ?", (t["id"],))
    llamadas = []
    original = trabajador.procesador.procesar_evento
    monkeypatch.setattr(trabajador.procesador, "procesar_evento",
                        lambda *a, **k: llamadas.append(1) or original(*a, **k))
    api = ApiFalsa([paciente("pac_1")], {"pac_1": []})
    assert recuperar_expirados(con, t0 + timedelta(seconds=61)) == 1
    procesar(ruta_db, contexto(api), t0 + timedelta(seconds=61))  # "nuevo proceso"
    t = trabajo_de(con, i)
    assert t["estado"] == "hecho" and t["intentos"] == 2
    assert llamadas == []  # no se repitió la primera etapa
    assert enr(con, i)["estado_enriquecimiento"] == "paciente"


def test_presupuesto_de_30_s_con_api_lenta(cliente, con, ruta_db):
    reloj = Reloj()
    # Cada respuesta tarda 4 s; con muchas páginas habría 10 peticiones (40 s).
    api = ApiFalsa([paciente(f"pac_x{k}", f"Otro {k}") for k in range(12)] + [paciente("pac_1")],
                   {"pac_1": [tratamiento(f"t{k}") for k in range(15)]}, reloj=reloj, demora_s=4)
    i = recibir(cliente, lead())
    inicio = reloj()
    procesar(ruta_db, contexto(api, reloj=reloj))
    assert reloj() - inicio <= 30 + 1e-9
    timeouts = [r.extensions["timeout"] for r in api.peticiones]
    assert all(t["read"] <= 5 and t["connect"] <= 3 for t in timeouts)
    assert timeouts[-1]["read"] < 5  # la última usó solo el tiempo que quedaba
    e = enr(con, i)
    assert e["estado_enriquecimiento"] == "no_disponible"
    assert trabajo_de(con, i)["estado"] == "hecho"


# --- Configuración ------------------------------------------------------------------------

def test_no_configurado_sin_llamadas_y_aviso_unico(ruta_db, caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    with TestClient(create_app(ruta_db)) as c:
        i = recibir(c, lead())
        procesar_pendientes(ruta_db, ahora())
        procesar_pendientes(ruta_db, ahora())
    avisos = [r for r in caplog.records if "API de pacientes no configurada" in r.getMessage()]
    assert len(avisos) == 1
    with sqlite3.connect(ruta_db) as c2:
        c2.row_factory = sqlite3.Row
        e = c2.execute("SELECT * FROM enriquecimientos WHERE evento_id = ?", (i,)).fetchone()
    assert e["estado_enriquecimiento"] == "no_configurado" and e["motivo"]


def test_configurada_no_avisa(ruta_db, caplog, monkeypatch):
    monkeypatch.setenv("API_PACIENTES_URL", API)
    monkeypatch.setenv("API_PACIENTES_TOKEN", TOKEN)
    caplog.set_level(logging.WARNING)
    create_app(ruta_db)
    assert "no configurada" not in caplog.text


@pytest.mark.parametrize("nombre, valor", [
    ("API_PACIENTES_URL", "ftp://api.test"),
    ("API_PACIENTES_URL", "no-es-url"),
    ("API_PACIENTES_URL", "https://usuario:clave@api.test"),
    ("API_PACIENTES_TOKEN", "con espacio"),
    ("PACIENTES_LIMITE_POR_MIN", "31"),
    ("PACIENTES_LIMITE_POR_MIN", "0"),
    ("PACIENTES_MAX_PAGINAS", "0"),
    ("PACIENTES_MAX_PAGINAS", "50"),
])
def test_config_pacientes_invalida_no_arranca(tmp_path, monkeypatch, nombre, valor):
    monkeypatch.setenv(nombre, valor)
    with pytest.raises(config.ConfigError, match=nombre) as exc:
        create_app(tmp_path / "leads.db")
    assert "clave" not in str(exc.value) and "con espacio" not in str(exc.value)


def test_config_pacientes_defectos():
    cfg = config.cargar_config({})
    assert (cfg.api_pacientes_url, cfg.api_pacientes_token, cfg.pacientes_limite_por_min,
            cfg.pacientes_max_paginas, cfg.pacientes_configurada) == (None, None, 25, 5, False)


# --- Red bloqueada -------------------------------------------------------------------------

def test_conftest_bloquea_red_externa():
    with pytest.raises(RedExternaBloqueada):
        socket.create_connection(("203.0.113.10", 443), timeout=1)
    with pytest.raises(RedExternaBloqueada):
        socket.getaddrinfo("api.example.com", 443)


def test_cliente_real_sin_transport_no_sale_de_la_maquina(cliente, con, ruta_db):
    """Sin MockTransport, httpx intentaría resolver el host: el bloqueo lo convierte en
    'error_de_red' y el lead degrada, sin salir a la red."""
    cfg = config.cargar_config({"API_PACIENTES_URL": "https://api-real.example.com",
                                "API_PACIENTES_TOKEN": TOKEN})
    ctx = trabajador.crear_contexto(cfg)
    i = recibir(cliente, lead())
    try:
        procesar(ruta_db, ctx)
    finally:
        ctx.cerrar()
    assert enr(con, i)["estado_enriquecimiento"] == "no_disponible"


# --- Bloque 3.1: D7.1, cruce por nombre ------------------------------------------------------

@pytest.mark.parametrize("nombre_lead, nombres_api, esperado", [
    ("Carla Mendoza", ["Carla Mendoza Ruiz"], "pac_0"),           # ejemplo del contrato
    ("Mendoza Carla", ["Carla Mendoza"], "pac_0"),                 # orden invertido
    ("María López", ["Maria Lopez Vera"], "pac_0"),                # tildes
    ("Carla Mendoza Ruiz", ["Carla Mendoza"], "pac_0"),            # simétrico: el largo en el lead
    ("Juan Perez", ["Juan Perez Lopez", "Juan Perez Gomez"], None),
    ("Juan Perez", ["Juan Perez Lopez", "Juan Perez"], None),      # D7.2: dos compatibles
    ("Carla", ["Carla Mendoza"], None),                         # una sola palabra
    ("", ["Carla Mendoza"], None),                              # vacío
    ("Carla Mendoza", ["Rosa Quishpe"], None),                  # sin relación
])
def test_d71_cruce_por_nombre(cliente, con, ruta_db, nombre_lead, nombres_api, esperado):
    pacientes = [paciente(f"pac_{k}", nombre) for k, nombre in enumerate(nombres_api)]
    api = ApiFalsa(pacientes, {p["id"]: [tratamiento("t1")] for p in pacientes})
    i = recibir(cliente, lead(nombre=nombre_lead))
    procesar(ruta_db, contexto(api))
    e = enr(con, i)
    pidio_tratamientos = [r for r in api.rutas() if r.endswith("/tratamientos")]
    if esperado is None:
        assert e["estado_enriquecimiento"] == "ambigua" and e["coincidencia"] == "ambigua"
        assert e["paciente_id"] is None and pidio_tratamientos == []
    else:
        assert e["estado_enriquecimiento"] == "paciente" and e["coincidencia"] == "unica"
        assert e["paciente_id"] == esperado
        assert pidio_tratamientos == [f"/v1/pacientes/{esperado}/tratamientos"]
        assert e["tratamientos_pendientes"] == 1
