"""Bloque 1: recepción, persistencia antes del ack e idempotencia por event_id."""
import json
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import db
from app import main as app_main
from app.main import create_app, parsear_desde

PAYLOADS = Path(__file__).resolve().parents[2] / "payloads"
URL = "/webhook/leads"


def payload(n: int) -> bytes:
    return (PAYLOADS / f"evento_{n:02d}.json").read_bytes()


def contar(ruta: Path, tabla: str) -> int:
    with sqlite3.connect(ruta) as con:
        return con.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


def filas(ruta: Path, sql: str, *args) -> list[sqlite3.Row]:
    con = sqlite3.connect(ruta)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


@pytest.fixture
def ruta_db(tmp_path) -> Path:
    return tmp_path / "leads.db"


@pytest.fixture
def cliente(ruta_db):
    with TestClient(create_app(ruta_db)) as c:
        yield c


@pytest.fixture
def servidor(ruta_db):
    """uvicorn real en un hilo, para tener concurrencia de verdad."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    puerto = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(ruta_db), log_level="warning"))
    hilo = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    hilo.start()
    limite = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < limite, "uvicorn no arrancó"
        time.sleep(0.02)
    yield f"http://127.0.0.1:{puerto}"
    server.should_exit = True
    hilo.join(10)
    sock.close()


# 1
def test_persistido_antes_del_ack(cliente, ruta_db):
    r = cliente.post(URL, content=payload(1))
    assert r.status_code == 202
    assert r.json()["estado"] == "pendiente"

    # Una instancia nueva de la app sobre el mismo archivo ve el evento.
    with TestClient(create_app(ruta_db)) as otro:
        r2 = otro.get("/leads/estado", params={"event_id": "evt_01JB7Q3M2X4K8R9T5W6Y"})
    assert r2.status_code == 200
    assert r2.json()["estado"] == "pendiente"

    # Y el cuerpo quedó byte a byte, con su trabajo.
    [fila] = filas(ruta_db, "SELECT * FROM eventos")
    assert fila["cuerpo_crudo"] == payload(1)
    assert fila["created_at_origen"] == "2026-11-03T14:22:05Z"
    assert contar(ruta_db, "trabajos") == 1


# 2
def test_mismo_event_id_dos_veces(cliente, ruta_db):
    r1 = cliente.post(URL, content=payload(1))
    r2 = cliente.post(URL, content=payload(2))
    assert r1.status_code == 202
    assert r2.status_code == 200
    assert r2.json()["estado"] == "pendiente"
    assert r2.json()["recepcion_id"] == r1.json()["recepcion_id"]
    assert contar(ruta_db, "eventos") == 1
    assert contar(ruta_db, "trabajos") == 1
    [rep] = filas(ruta_db, "SELECT * FROM repeticiones")
    assert rep["event_id"] == "evt_01JB7Q3M2X4K8R9T5W6Y"
    assert rep["mismo_contenido"] == 1


# 3
def test_concurrencia_mismo_event_id(servidor, ruta_db):
    n = 20
    barrera = threading.Barrier(n)
    cuerpo = payload(1)

    def enviar(_):
        with httpx.Client(timeout=15) as c:
            barrera.wait()
            return c.post(servidor + URL, content=cuerpo).status_code

    with ThreadPoolExecutor(n) as ex:
        codigos = list(ex.map(enviar, range(n)))

    assert sorted(codigos) == [200] * (n - 1) + [202]
    assert contar(ruta_db, "eventos") == 1
    assert contar(ruta_db, "trabajos") == 1
    assert contar(ruta_db, "repeticiones") == n - 1


# 4
def test_mismo_event_id_contenido_distinto(cliente, ruta_db):
    original = payload(1)
    alterado = json.loads(original)
    alterado["lead"]["message"] = "Otro mensaje distinto"
    alterado = json.dumps(alterado).encode()

    assert cliente.post(URL, content=original).status_code == 202
    r = cliente.post(URL, content=alterado)
    assert r.status_code == 200

    [ev] = filas(ruta_db, "SELECT * FROM eventos")
    assert ev["cuerpo_crudo"] == original
    [rep] = filas(ruta_db, "SELECT * FROM repeticiones")
    assert rep["mismo_contenido"] == 0
    assert rep["cuerpo_crudo"] == alterado
    assert rep["hash_cuerpo"] != ev["hash_cuerpo"]


# 5
@pytest.mark.parametrize("cuerpo, motivo", [
    (b"esto no es json {", "El cuerpo no es JSON válido"),
    (b'[{"event_id": "evt_lista"}]', "El JSON recibido no es un objeto"),
    (b"\x80\x81\x82", "El cuerpo no es JSON válido"),
    (b'{"event_id": "evt_\xff\xfe"}', "El cuerpo no es JSON válido"),
])
def test_cuerpo_invalido_se_guarda_rechazado(cliente, ruta_db, cuerpo, motivo):
    r = cliente.post(URL, content=cuerpo)
    assert 200 <= r.status_code < 300
    assert r.json()["estado"] == "rechazado"
    [ev] = filas(ruta_db, "SELECT * FROM eventos")
    assert ev["estado"] == "rechazado"
    assert ev["motivo"] == motivo
    assert ev["event_id"] is None
    assert ev["cuerpo_crudo"] == cuerpo
    assert contar(ruta_db, "trabajos") == 0


def test_event_id_numerico_identico_es_un_solo_evento(cliente, ruta_db):
    cuerpo = json.dumps({"event_id": 12345, "lead": {}}).encode()
    assert cliente.post(URL, content=cuerpo).status_code == 202
    assert cliente.post(URL, content=cuerpo).status_code == 200
    [ev] = filas(ruta_db, "SELECT * FROM eventos")
    assert ev["event_id"] == "12345"
    assert contar(ruta_db, "trabajos") == 1
    assert cliente.get("/leads/estado", params={"event_id": "12345"}).status_code == 200


def test_event_id_con_espacios_es_el_mismo_evento(cliente, ruta_db):
    assert cliente.post(URL, content=b'{"event_id": " evt_x "}').status_code == 202
    assert cliente.post(URL, content=b'{"event_id": "evt_x"}').status_code == 200
    [ev] = filas(ruta_db, "SELECT * FROM eventos")
    assert ev["event_id"] == "evt_x"
    assert contar(ruta_db, "repeticiones") == 1
    r = cliente.get("/leads/estado", params={"event_id": "  evt_x "})
    assert r.status_code == 200
    assert r.json()["event_id"] == "evt_x"


@pytest.mark.parametrize("valor", [True, False, 1.5, None, "", "   ", ["evt"], {"id": "evt"}])
def test_event_id_de_otro_tipo_o_vacio_queda_null(cliente, ruta_db, valor):
    cuerpo = json.dumps({"event_id": valor}).encode()
    assert cliente.post(URL, content=cuerpo).status_code == 202
    [ev] = filas(ruta_db, "SELECT * FROM eventos")
    assert ev["event_id"] is None


def test_event_id_con_sustituto_suelto_es_idempotente(cliente, ruta_db):
    cuerpo = b'{"event_id": "evt_\\ud800"}'
    assert cliente.post(URL, content=cuerpo).status_code == 202
    assert cliente.post(URL, content=cuerpo).status_code == 200
    [ev] = filas(ruta_db, "SELECT * FROM eventos")
    assert ev["event_id"] == "evt_\\ud800"
    assert ev["cuerpo_crudo"] == cuerpo
    assert contar(ruta_db, "repeticiones") == 1


def test_created_at_con_sustituto_suelto(cliente, ruta_db):
    cuerpo = b'{"event_id": "evt_ca", "created_at": "2026-11-03\\ud800"}'
    assert cliente.post(URL, content=cuerpo).status_code == 202
    assert cliente.post(URL, content=cuerpo).status_code == 200
    [ev] = filas(ruta_db, "SELECT * FROM eventos")
    assert ev["created_at_origen"] == "2026-11-03\\ud800"
    assert ev["estado"] == "pendiente"


def test_excepcion_al_extraer_guarda_pendiente_con_trabajo(cliente, ruta_db, monkeypatch, caplog):
    def explota(_valor):
        raise RuntimeError("fallo forzado con dato carla.mendoza@example.com")

    monkeypatch.setattr(app_main, "normalizar_event_id", explota)
    r = cliente.post(URL, content=payload(1))
    assert r.status_code == 202
    assert r.json()["estado"] == "pendiente"
    [ev] = filas(ruta_db, "SELECT * FROM eventos")
    assert ev["estado"] == "pendiente"
    assert ev["event_id"] is None
    assert ev["motivo"] is None
    assert ev["cuerpo_crudo"] == payload(1)
    [t] = filas(ruta_db, "SELECT * FROM trabajos")
    assert t["evento_id"] == ev["id"] and t["estado"] == "pendiente"
    # Solo el tipo de error en el log, nunca el mensaje.
    assert "RuntimeError" in caplog.text
    assert "carla" not in caplog.text.lower()


def test_si_tambien_falla_el_guardado_de_respaldo_da_503(cliente, ruta_db, monkeypatch):
    def explota(*_a, **_k):
        raise RuntimeError("fallo forzado")

    monkeypatch.setattr(db, "registrar_recepcion", explota)
    assert cliente.post(URL, content=payload(1)).status_code == 503
    assert contar(ruta_db, "eventos") == 0


# 6
def test_dos_eventos_sin_event_id(cliente, ruta_db):
    cuerpo = json.dumps({"event_type": "lead.created", "lead": {"full_name": "X"}}).encode()
    assert cliente.post(URL, content=cuerpo).status_code == 202
    assert cliente.post(URL, content=cuerpo).status_code == 202
    assert contar(ruta_db, "eventos") == 2
    assert contar(ruta_db, "trabajos") == 2

    r = cliente.get("/leads/estado", params={"desde": "2000-01-01T00:00:00Z"})
    assert r.status_code == 200
    lista = r.json()
    assert len(lista) == 2
    assert all(e["event_id"] is None and e["estado"] == "pendiente" for e in lista)
    assert set(lista[0]) == set(db.CAMPOS_CONTRATO) | {"recepcion_id"}
    assert lista[0]["recepcion_id"] != lista[1]["recepcion_id"]


# 7
def test_fallo_de_conexion_da_503(cliente, ruta_db, monkeypatch):
    def roto(_ruta):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "conectar", roto)
    r = cliente.post(URL, content=payload(1))
    assert r.status_code == 503
    assert cliente.get("/leads/estado", params={"event_id": "x"}).status_code == 503


def test_fallo_a_mitad_de_transaccion_da_503_y_no_deja_evento(cliente, ruta_db):
    with sqlite3.connect(ruta_db) as con:
        con.execute("DROP TABLE trabajos")
    r = cliente.post(URL, content=payload(1))
    assert r.status_code == 503
    assert contar(ruta_db, "eventos") == 0


# 8
def test_404_y_400(cliente):
    assert cliente.get("/leads/estado", params={"event_id": "evt_no_existe"}).status_code == 404
    assert cliente.get("/leads/estado", params={"desde": "ayer"}).status_code == 400
    assert cliente.get("/leads/estado", params={"desde": "2026-13-45T99:00:00Z"}).status_code == 400
    assert cliente.get("/leads/estado").status_code == 400


@pytest.mark.parametrize("desde", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_desde_fuera_de_rango_da_400(cliente, desde):
    assert cliente.get("/leads/estado", params={"desde": desde}).status_code == 400


def test_desde_filtra_y_convierte_zonas(cliente):
    cliente.post(URL, content=payload(3))
    assert len(cliente.get("/leads/estado", params={"desde": "2000-01-01T00:00:00-05:00"}).json()) == 1
    assert cliente.get("/leads/estado", params={"desde": "2999-01-01T00:00:00"}).json() == []


def test_parsear_desde():
    assert parsear_desde("2026-11-03T10:00:00-05:00") == "2026-11-03T15:00:00.000000Z"
    assert parsear_desde("2026-11-03T15:00:00Z") == "2026-11-03T15:00:00.000000Z"
    assert parsear_desde("2026-11-03T15:00:00") == "2026-11-03T15:00:00.000000Z"
    # '+' sin codificar en la URL llega como espacio
    assert parsear_desde("2026-11-03T20:00:00 05:00") == "2026-11-03T15:00:00.000000Z"
    with pytest.raises(ValueError):
        parsear_desde("03/11/2026")


def test_salud(cliente):
    assert cliente.get("/salud").json() == {"status": "ok"}
