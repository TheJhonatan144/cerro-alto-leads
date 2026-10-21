"""Paso 4 (5b, D20): vigilancia interna con reloj falso."""
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import config, db, despachador, errores, trabajador, vigilancia
from app.despachador import Despachador


@pytest.fixture
def base(tmp_path):
    ruta = tmp_path / "leads.db"
    db.inicializar(ruta)
    errores.configurar(ruta, ventana_s=900)
    yield ruta
    errores.desconfigurar()


def incidencias(ruta):
    c = sqlite3.connect(ruta)
    c.row_factory = sqlite3.Row
    try:
        return c.execute("SELECT * FROM incidencias ORDER BY id").fetchall()
    finally:
        c.close()


def _evento(ruta, recibido: datetime, estado="pendiente"):
    c = db.conectar(ruta)
    try:
        r = db.registrar_recepcion(c, cuerpo=b"{}", event_id=None, created_at_origen=None, estado="pendiente",
                                   motivo=None)
        motivo = None if estado == "pendiente" else "x"
        c.execute("UPDATE eventos SET received_at = ?, estado = ?, motivo = ? WHERE recepcion_id = ?",
                  (recibido.strftime(db.FORMATO_UTC), estado, motivo, r.recepcion_id))
    finally:
        c.close()


CFG = config.cargar_config({"PENDIENTE_ANTIGUO_S": "900", "VIGILANCIA_CADA_S": "60"})
AHORA = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


def test_pendiente_antiguo_con_conteo_y_antiguedad(base):
    _evento(base, AHORA - timedelta(seconds=1000))
    _evento(base, AHORA - timedelta(seconds=2000))
    _evento(base, AHORA - timedelta(seconds=100))           # reciente: no cuenta
    _evento(base, AHORA - timedelta(seconds=5000), "rechazado")  # no pendiente: no cuenta
    c = db.conectar(base)
    try:
        vigilancia.vigilar(c, AHORA, CFG, {"trabajador": True, "despachador": True})
    finally:
        c.close()
    [i] = incidencias(base)
    assert i["codigo"] == "pendiente_antiguo"
    assert i["detalle"] == "origen=vigilancia conteo=2 antiguedad_max_s=2000"


def test_sin_pendientes_antiguos_no_reporta(base):
    _evento(base, AHORA - timedelta(seconds=800))
    c = db.conectar(base)
    try:
        vigilancia.vigilar(c, AHORA, CFG, {"trabajador": True})
    finally:
        c.close()
    assert incidencias(base) == []


def test_deduplica_por_la_ventana_de_incidencias(base):
    _evento(base, AHORA - timedelta(seconds=1000))
    c = db.conectar(base)
    try:
        for k in range(3):
            vigilancia.vigilar(c, AHORA + timedelta(seconds=60 * k), CFG, None)
    finally:
        c.close()
    [i] = incidencias(base)
    assert i["ocurrencias"] == 3


def test_hilo_caido_con_su_nombre(base):
    c = db.conectar(base)
    try:
        vigilancia.vigilar(c, AHORA, CFG, {"trabajador": False, "despachador": True})
    finally:
        c.close()
    [i] = incidencias(base)
    assert i["codigo"] == "hilo_caido" and i["clave"] == "trabajador" and i["detalle"] == "origen=trabajador"


def test_no_duplica_los_codigos_de_fallidos(base):
    _evento(base, AHORA - timedelta(seconds=5000), "fallido")
    c = db.conectar(base)
    try:
        vigilancia.vigilar(c, AHORA, CFG, {"trabajador": True})
    finally:
        c.close()
    assert incidencias(base) == []  # los fallidos ya tienen evento_fallido / aviso_fallido


def test_el_despachador_vigila_cada_n_segundos(base, monkeypatch):
    llamadas = []
    monkeypatch.setattr(vigilancia, "vigilar", lambda con, ahora, cfg, hilos: llamadas.append(ahora))
    d = Despachador(base, canal=None, cfg=CFG, hilos=lambda: {"trabajador": True})
    for segundos in (0, 30, 59, 60, 61, 119, 125):
        d._tal_vez_vigilar(AHORA + timedelta(seconds=segundos))
    assert [int((t - AHORA).total_seconds()) for t in llamadas] == [0, 60, 125]


def test_el_bucle_del_despachador_vigila(base, monkeypatch):
    _evento(base, datetime.now(timezone.utc) - timedelta(seconds=5000))

    class Ok:
        nombre = "ok"

        def enviar(self, *a, **k):
            pass

    d = Despachador(base, Ok(), cfg=CFG, hilos=lambda: {"trabajador": False}, intervalo_s=0.05)
    d.iniciar()
    try:
        limite = time.monotonic() + 5
        while time.monotonic() < limite and len({i["codigo"] for i in incidencias(base)}) < 2:
            time.sleep(0.05)
    finally:
        d.detener(timeout=5)
    assert {i["codigo"] for i in incidencias(base)} == {"pendiente_antiguo", "hilo_caido"}


def test_el_trabajador_vigila_al_despachador(base):
    t = trabajador.Trabajador(base, config.cargar_config({"WORKER_INTERVALO_S": "0.1", "VIGILANCIA_CADA_S": "10"}),
                              hilos=lambda: {"despachador": False})
    t.iniciar()
    try:
        limite = time.monotonic() + 5
        while time.monotonic() < limite and not incidencias(base):
            time.sleep(0.05)
    finally:
        t.detener(timeout=5)
    [i] = incidencias(base)
    assert i["codigo"] == "hilo_caido" and i["clave"] == "despachador"


@pytest.mark.parametrize("variable, valor", [
    ("VIGILANCIA_CADA_S", "9"), ("VIGILANCIA_CADA_S", "3601"),
    ("PENDIENTE_ANTIGUO_S", "59"), ("PENDIENTE_ANTIGUO_S", "86401"),
    ("ERRORES_VENTANA_S", "59"), ("ERRORES_VENTANA_S", "x"),
])
def test_rangos_de_vigilancia(variable, valor):
    with pytest.raises(config.ConfigError, match=variable):
        config.cargar_config({variable: valor})


def test_sin_falso_hilo_caido_al_arrancar(tmp_path, monkeypatch):
    """Prueba final del 5b: el trabajador vigilaba al despachador antes de que este arrancara."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    monkeypatch.setenv("WORKER_ACTIVO", "1")
    monkeypatch.setenv("VIGILANCIA_CADA_S", "10")
    ruta = tmp_path / "leads.db"
    with TestClient(create_app(ruta)):
        time.sleep(1.0)
    assert [i["codigo"] for i in incidencias(ruta) if i["codigo"] == "hilo_caido"] == []
