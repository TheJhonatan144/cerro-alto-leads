"""Bloque 6.1: reproceso de eventos fallidos (CLI listar-fallidos y reprocesar)."""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cli, db, trabajador
from app.despachador import despachar_pendientes
from app.main import create_app
from app.trabajador import Etapa, procesar_pendientes

PAYLOADS = Path(__file__).resolve().parents[2] / "payloads"
PAYLOAD_01 = (PAYLOADS / "evento_01.json").read_bytes()  # caliente con LLM_PROVIDER=none
EVT_01 = "evt_01JB7Q3M2X4K8R9T5W6Y"
PAYLOAD_07 = (PAYLOADS / "evento_07.json").read_bytes()  # caliente
EVT_07 = "evt_01JB7RH3B5D7F9H1K3M5P7"
URL = "/webhook/leads"
DATOS_PERSONALES = ("carla", "mendoza", "0991234567", "+593991234567", "@example.com")


class CanalFalso:
    nombre = "falso"

    def __init__(self):
        self.enviados: list[int] = []

    def enviar(self, texto, aviso, presupuesto=None):
        self.enviados.append(aviso["evento_id"])


@pytest.fixture
def ruta_db(tmp_path) -> Path:
    return tmp_path / "leads.db"


@pytest.fixture
def cliente(ruta_db):
    with TestClient(create_app(ruta_db)) as c:  # WORKER_ACTIVO=0 (conftest)
        yield c


@pytest.fixture
def con(ruta_db, cliente):
    c = db.conectar(ruta_db)
    yield c
    c.close()


def recibir(cliente, cuerpo) -> int:
    r = cliente.post(URL, content=cuerpo)
    assert r.status_code in (200, 202)
    with sqlite3.connect(cliente.app.state.db_path) as c:
        return c.execute("SELECT id FROM eventos WHERE recepcion_id = ?",
                         (r.json()["recepcion_id"],)).fetchone()[0]


def evento(con, i):
    return con.execute("SELECT * FROM eventos WHERE id = ?", (i,)).fetchone()


def trabajo_de(con, i):
    return con.execute("SELECT * FROM trabajos WHERE evento_id = ?", (i,)).fetchone()


def _explota(*_a, **_k):
    raise RuntimeError("Carla Mendoza 0991234567")  # el mensaje lleva datos personales


def forzar_fallido(ruta_db, monkeypatch) -> datetime:
    """La etapa de clasificación falla siempre: el trabajo agota sus 5 intentos y el evento
    queda 'fallido' con la etapa enriquecer_paciente ya completada. Devuelve el instante final."""
    rota = [e if e.nombre != "clasificar_lead" else Etapa(e.nombre, _explota) for e in trabajador.ETAPAS]
    momento = datetime.now(timezone.utc) + timedelta(seconds=1)
    with monkeypatch.context() as m:
        m.setattr(trabajador, "ETAPAS", rota)
        for _ in range(5):
            procesar_pendientes(ruta_db, momento)
            momento += timedelta(hours=1)  # más que cualquier espera de reintento
    return momento


def volcado(ruta_db) -> list[str]:
    c = sqlite3.connect(ruta_db)
    try:
        return list(c.iterdump())
    finally:
        c.close()


def avisos(con, i) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM avisos WHERE evento_id = ?", (i,)).fetchall()


# --- Preparación: el evento fallido ------------------------------------------------------------

def test_preparacion_el_evento_queda_fallido_tras_cinco_intentos(cliente, con, ruta_db, monkeypatch):
    i = recibir(cliente, PAYLOAD_01)
    forzar_fallido(ruta_db, monkeypatch)
    t = trabajo_de(con, i)
    assert (t["estado"], t["intentos"], t["etapa"]) == ("fallido", 5, "enriquecer_paciente")
    assert evento(con, i)["estado"] == "fallido" and avisos(con, i) == []


# --- listar-fallidos ---------------------------------------------------------------------------

def test_listar_fallidos_sin_datos_personales(cliente, con, ruta_db, monkeypatch, capsys):
    i = recibir(cliente, PAYLOAD_01)
    forzar_fallido(ruta_db, monkeypatch)
    capsys.readouterr()
    assert cli.main(["--db", str(ruta_db), "listar-fallidos"]) == 0
    out = capsys.readouterr().out
    assert EVT_01 in out and "enriquecer_paciente" in out and "5" in out
    assert not any(p in out.lower() for p in DATOS_PERSONALES)
    assert evento(con, i)["estado"] == "fallido"  # listar no cambia nada


def test_listar_fallidos_vacio(ruta_db, capsys):
    db.inicializar(ruta_db)
    assert cli.main(["--db", str(ruta_db), "listar-fallidos"]) == 0
    assert "No hay eventos fallidos" in capsys.readouterr().out


# --- reprocesar --------------------------------------------------------------------------------

def test_reprocesar_recupera_el_evento_con_un_unico_aviso(cliente, con, ruta_db, monkeypatch, capsys):
    i = recibir(cliente, PAYLOAD_01)
    momento = forzar_fallido(ruta_db, monkeypatch)

    assert cli.main(["--db", str(ruta_db), "reprocesar", "--event-id", EVT_01]) == 0
    out = capsys.readouterr().out
    assert "Eventos devueltos a pendiente: 1" in out
    assert not any(p in out.lower() for p in DATOS_PERSONALES)
    e, t = evento(con, i), trabajo_de(con, i)
    assert (e["estado"], e["motivo"], e["ruta"]) == ("pendiente", None, None)
    assert (t["estado"], t["intentos"], t["lease_hasta"], t["ultimo_error"]) == ("pendiente", 0, None, None)
    assert t["etapa"] == "enriquecer_paciente"  # se reanuda donde se quedó (etapas idempotentes)
    assert cliente.get("/leads/estado", params={"event_id": EVT_01}).json()["estado"] == "pendiente"

    procesar_pendientes(ruta_db, momento)
    e = evento(con, i)
    assert (e["estado"], e["ruta"]) == ("procesado", "caliente")
    assert trabajo_de(con, i)["estado"] == "hecho"
    assert len(avisos(con, i)) == 1
    canal = CanalFalso()
    despachar_pendientes(ruta_db, canal, momento)
    assert canal.enviados == [i]


def test_reprocesar_dos_veces_seguidas_no_duplica(cliente, con, ruta_db, monkeypatch, capsys):
    i = recibir(cliente, PAYLOAD_01)
    j = recibir(cliente, PAYLOAD_07)
    momento = forzar_fallido(ruta_db, monkeypatch)
    assert evento(con, j)["estado"] == "fallido"

    assert cli.main(["--db", str(ruta_db), "reprocesar", "--todos"]) == 0
    assert "Eventos devueltos a pendiente: 2" in capsys.readouterr().out
    assert cli.main(["--db", str(ruta_db), "reprocesar", "--todos"]) == 0
    assert "Eventos devueltos a pendiente: 0" in capsys.readouterr().out
    procesar_pendientes(ruta_db, momento)
    # Y una tercera vez, ya procesados: nada que reprocesar y nada se duplica.
    assert cli.main(["--db", str(ruta_db), "reprocesar", "--todos"]) == 0
    assert "Eventos devueltos a pendiente: 0" in capsys.readouterr().out
    assert cli.main(["--db", str(ruta_db), "reprocesar", "--event-id", EVT_01]) == 0
    assert "no está fallido (estado: procesado)" in capsys.readouterr().out
    procesar_pendientes(ruta_db, momento + timedelta(hours=1))

    for k in (i, j):
        assert evento(con, k)["estado"] == "procesado" and len(avisos(con, k)) == 1
        assert con.execute("SELECT COUNT(*) FROM citas_tentativas WHERE evento_id = ?", (k,)).fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM trabajos").fetchone()[0] == 2
    canal = CanalFalso()
    despachar_pendientes(ruta_db, canal, momento + timedelta(hours=1))
    assert sorted(canal.enviados) == sorted([i, j])


def test_reprocesar_tibio_no_duplica_seguimientos(cliente, con, ruta_db, monkeypatch):
    i = recibir(cliente, (PAYLOADS / "evento_04.json").read_bytes())  # tibio
    momento = forzar_fallido(ruta_db, monkeypatch)
    for _ in range(2):
        assert cli.main(["--db", str(ruta_db), "reprocesar", "--todos"]) == 0
        procesar_pendientes(ruta_db, momento)
    assert (evento(con, i)["estado"], evento(con, i)["ruta"]) == ("procesado", "tibio")
    assert con.execute("SELECT COUNT(*) FROM seguimientos WHERE evento_id = ?", (i,)).fetchone()[0] == 1


def test_dry_run_no_cambia_nada(cliente, con, ruta_db, monkeypatch, capsys):
    recibir(cliente, PAYLOAD_01)
    forzar_fallido(ruta_db, monkeypatch)
    antes = volcado(ruta_db)
    capsys.readouterr()
    assert cli.main(["--db", str(ruta_db), "reprocesar", "--todos", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "simulación" in out and EVT_01 in out and "se devolverían a pendiente: 1" in out
    assert cli.main(["--db", str(ruta_db), "reprocesar", "--event-id", EVT_01, "--dry-run"]) == 0
    assert volcado(ruta_db) == antes


def test_event_id_inexistente(cliente, con, ruta_db, monkeypatch, capsys):
    recibir(cliente, PAYLOAD_01)
    forzar_fallido(ruta_db, monkeypatch)
    antes = volcado(ruta_db)
    capsys.readouterr()
    assert cli.main(["--db", str(ruta_db), "reprocesar", "--event-id", "evt_no_existe"]) == 1
    captura = capsys.readouterr()
    assert "event_id no encontrado" in captura.err and "evt_no_existe" not in captura.err
    assert cli.main(["--db", str(ruta_db), "reprocesar", "--event-id", "evt_no_existe", "--dry-run"]) == 1
    assert volcado(ruta_db) == antes


def test_exige_event_id_o_todos(ruta_db):
    db.inicializar(ruta_db)
    with pytest.raises(SystemExit):
        cli.main(["--db", str(ruta_db), "reprocesar"])
    with pytest.raises(SystemExit):
        cli.main(["--db", str(ruta_db), "reprocesar", "--todos", "--event-id", EVT_01])


def test_no_pisa_lease_activa(cliente, con, ruta_db, monkeypatch, capsys):
    """Un trabajo 'en_curso' con lease vigente pertenece a un trabajador vivo: no se toca."""
    i = recibir(cliente, PAYLOAD_01)
    forzar_fallido(ruta_db, monkeypatch)
    futuro = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime(db.FORMATO_UTC)
    con.execute("UPDATE trabajos SET estado = 'en_curso', lease_hasta = ?, intentos = 3 WHERE evento_id = ?",
                (futuro, i))
    capsys.readouterr()
    assert cli.main(["--db", str(ruta_db), "reprocesar", "--todos"]) == 0
    out = capsys.readouterr().out
    assert "Eventos devueltos a pendiente: 0" in out and "en curso" in out
    t = trabajo_de(con, i)
    assert (t["estado"], t["lease_hasta"], t["intentos"]) == ("en_curso", futuro, 3)
    assert evento(con, i)["estado"] == "fallido"


def test_lease_vencida_si_se_reprocesa(cliente, con, ruta_db, monkeypatch):
    i = recibir(cliente, PAYLOAD_01)
    forzar_fallido(ruta_db, monkeypatch)
    pasado = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(db.FORMATO_UTC)
    con.execute("UPDATE trabajos SET estado = 'en_curso', lease_hasta = ? WHERE evento_id = ?", (pasado, i))
    assert cli.main(["--db", str(ruta_db), "reprocesar", "--todos"]) == 0
    assert trabajo_de(con, i)["estado"] == "pendiente" and evento(con, i)["estado"] == "pendiente"


def test_evento_fallido_sin_trabajo_recibe_uno(cliente, con, ruta_db, monkeypatch):
    i = recibir(cliente, PAYLOAD_01)
    momento = forzar_fallido(ruta_db, monkeypatch)
    con.execute("DELETE FROM trabajos WHERE evento_id = ?", (i,))
    assert cli.main(["--db", str(ruta_db), "reprocesar", "--event-id", EVT_01]) == 0
    t = trabajo_de(con, i)
    assert (t["estado"], t["intentos"], t["etapa"]) == ("pendiente", 0, "pendiente")
    procesar_pendientes(ruta_db, momento)
    assert evento(con, i)["estado"] == "procesado" and len(avisos(con, i)) == 1


def test_es_una_sola_transaccion(cliente, con, ruta_db, monkeypatch):
    """Si falla a mitad (segundo evento), no queda ningún evento devuelto a pendiente."""
    i = recibir(cliente, PAYLOAD_01)
    j = recibir(cliente, PAYLOAD_07)
    forzar_fallido(ruta_db, monkeypatch)
    # Un disparador que rompe la segunda actualización de eventos.
    con.execute(f"""CREATE TRIGGER romper BEFORE UPDATE OF estado ON eventos
                    WHEN NEW.id = {j} AND NEW.estado = 'pendiente'
                    BEGIN SELECT RAISE(ABORT, 'roto'); END""")
    assert cli.main(["--db", str(ruta_db), "reprocesar", "--todos"]) == 2
    con.execute("DROP TRIGGER romper")
    assert evento(con, i)["estado"] == "fallido" and trabajo_de(con, i)["estado"] == "fallido"
    assert evento(con, j)["estado"] == "fallido" and trabajo_de(con, j)["estado"] == "fallido"


def test_base_inexistente_no_se_crea(tmp_path, capsys):
    ruta = tmp_path / "no.db"
    assert cli.main(["--db", str(ruta), "reprocesar", "--todos"]) == 2
    assert cli.main(["--db", str(ruta), "listar-fallidos"]) == 2
    assert not ruta.exists()


# --- Con el servidor corriendo (WAL) -----------------------------------------------------------

def test_reprocesar_con_el_servidor_corriendo(ruta_db, monkeypatch, tmp_path):
    archivo = tmp_path / "avisos.jsonl"
    monkeypatch.setenv("AVISOS_ARCHIVO", str(archivo))
    with TestClient(create_app(ruta_db)) as c:  # primero, sin hilos: se fuerza el fallido
        i = recibir(c, PAYLOAD_01)
        forzar_fallido(ruta_db, monkeypatch)
    with sqlite3.connect(ruta_db) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    monkeypatch.setenv("WORKER_ACTIVO", "1")
    with TestClient(create_app(ruta_db)) as c:
        assert c.app.state.trabajador.vivo() and c.app.state.despachador.vivo()
        assert cli.main(["--db", str(ruta_db), "listar-fallidos"]) == 0
        assert cli.main(["--db", str(ruta_db), "reprocesar", "--todos"]) == 0
        limite = time.monotonic() + 10
        while time.monotonic() < limite:
            if (c.get("/leads/estado", params={"event_id": EVT_01}).json()["estado"] == "procesado"
                    and archivo.exists() and archivo.read_text().strip()):
                break
            time.sleep(0.05)
        r = c.get("/leads/estado", params={"event_id": EVT_01}).json()
        assert (r["estado"], r["ruta"]) == ("procesado", "caliente")
        assert c.get("/leads/resumen").json()["eventos"]["por_estado"]["fallido"] == 0
    con = db.conectar(ruta_db)
    try:
        [aviso] = avisos(con, i)
        assert aviso["estado"] == "enviado"
    finally:
        con.close()
    lineas = [json.loads(x) for x in archivo.read_text().splitlines()]
    assert len(lineas) == 1
