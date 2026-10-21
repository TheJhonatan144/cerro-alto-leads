"""Bloque 2b: cola de trabajos, lease, reintentos, etapas e hilo trabajador."""
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, procesador, trabajador
from app.main import create_app
from app.trabajador import (
    ESPERAS_REINTENTO_S, ejecutar_trabajo, procesar_pendientes, reclamar_trabajo,
    recuperar_expirados,
)

PAYLOADS = Path(__file__).resolve().parents[2] / "payloads"
URL = "/webhook/leads"
PLAZO_S = 5


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


def cuerpo_lead(nombre="Carla Mendoza", phone="0991234567", email="carla@example.com"):
    return json.dumps({
        "event_id": f"evt_t_{uuid.uuid4().hex}", "event_type": "lead.created",
        "lead": {"full_name": nombre, "phone": phone, "email": email, "message": "Hola"},
    }).encode()


def recibir(cliente, cuerpo=None) -> int:
    r = cliente.post(URL, content=cuerpo or cuerpo_lead())
    assert r.status_code in (200, 202)
    with sqlite3.connect(cliente.app.state.db_path) as c:
        return c.execute("SELECT id FROM eventos WHERE recepcion_id = ?",
                         (r.json()["recepcion_id"],)).fetchone()[0]


def ahora_tras_recibir() -> datetime:
    # Un poco después de la recepción, para que proximo_intento_at ya haya vencido.
    return datetime.now(timezone.utc) + timedelta(seconds=1)


def trabajo_de(con, evento_id) -> sqlite3.Row:
    return con.execute("SELECT * FROM trabajos WHERE evento_id = ?", (evento_id,)).fetchone()


def evento(con, evento_id) -> sqlite3.Row:
    return con.execute("SELECT * FROM eventos WHERE id = ?", (evento_id,)).fetchone()


def leer_utc(texto: str) -> datetime:
    return datetime.strptime(texto, db.FORMATO_UTC).replace(tzinfo=timezone.utc)


def esperar(condicion, plazo=PLAZO_S):
    limite = time.monotonic() + plazo
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.05)
    return condicion()


def sin_trabajos_abiertos(ruta) -> bool:
    with sqlite3.connect(ruta) as c:
        return c.execute(
            "SELECT COUNT(*) FROM trabajos WHERE estado IN ('pendiente', 'en_curso')"
        ).fetchone()[0] == 0


@pytest.fixture
def etapa_rota(monkeypatch):
    """La etapa lanza una excepción cuyo mensaje lleva datos personales."""
    def explota(*_a, **_k):
        raise RuntimeError("Carla Mendoza 0991234567 carla@example.com")
    monkeypatch.setattr(procesador, "procesar_evento", explota)


# --- Reclamo -----------------------------------------------------------------

def test_dos_conexiones_reclaman_a_la_vez_solo_una_obtiene(cliente, ruta_db):
    recibir(cliente)
    ahora = ahora_tras_recibir()
    barrera = threading.Barrier(2)
    resultados = []

    def reclamar():
        c = db.conectar(ruta_db)
        try:
            barrera.wait()
            resultados.append(reclamar_trabajo(c, ahora))
        finally:
            c.close()

    hilos = [threading.Thread(target=reclamar) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    obtenidos = [r for r in resultados if r is not None]
    assert len(obtenidos) == 1
    assert obtenidos[0]["estado"] == "en_curso" and obtenidos[0]["intentos"] == 1


def test_reclamo_en_orden_de_id(cliente, con):
    ids = [recibir(cliente) for _ in range(3)]
    ahora = ahora_tras_recibir()
    reclamados = [reclamar_trabajo(con, ahora)["evento_id"] for _ in range(3)]
    assert reclamados == ids
    assert reclamar_trabajo(con, ahora) is None


def test_reclamo_fija_lease(cliente, con):
    recibir(cliente)
    ahora = ahora_tras_recibir()
    t = reclamar_trabajo(con, ahora, lease_s=60)
    assert leer_utc(t["lease_hasta"]) == ahora + timedelta(seconds=60)


# --- Éxito -------------------------------------------------------------------------

def test_exito_trabajo_hecho_y_lead_creado(cliente, con, ruta_db):
    i = recibir(cliente)
    assert procesar_pendientes(ruta_db, ahora_tras_recibir()) == 1
    t = trabajo_de(con, i)
    assert t["estado"] == "hecho"
    assert t["etapa"] == "normalizar_y_duplicados"
    assert t["intentos"] == 1 and t["lease_hasta"] is None
    e = evento(con, i)
    assert e["estado"] == "pendiente"  # las etapas siguientes llegan en otros bloques
    assert e["lead_id"] is not None and e["telefono_e164"] == "+593991234567"
    assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1


def test_etapa_ya_completada_no_se_repite(cliente, con, ruta_db, monkeypatch):
    i = recibir(cliente)
    con.execute("UPDATE trabajos SET etapa = 'normalizar_y_duplicados' WHERE evento_id = ?", (i,))
    llamadas = []
    monkeypatch.setattr(procesador, "procesar_evento", lambda *a, **k: llamadas.append(a))
    procesar_pendientes(ruta_db, ahora_tras_recibir())
    assert llamadas == []
    assert trabajo_de(con, i)["estado"] == "hecho"


def test_nuevas_etapas_se_ejecutan_en_orden(cliente, con, ruta_db, monkeypatch):
    orden = []
    monkeypatch.setattr(trabajador, "ETAPAS", [
        trabajador.Etapa("a", lambda *_: orden.append("a")),
        trabajador.Etapa("b", lambda *_: orden.append("b")),
    ])
    i = recibir(cliente)
    procesar_pendientes(ruta_db, ahora_tras_recibir())
    assert orden == ["a", "b"]
    assert trabajo_de(con, i)["etapa"] == "b"


def test_reanuda_tras_la_ultima_etapa_completada(cliente, con, ruta_db, monkeypatch):
    orden = []

    def b_rota(*_):
        orden.append("b")
        raise RuntimeError("x")

    monkeypatch.setattr(trabajador, "ETAPAS", [
        trabajador.Etapa("a", lambda *_: orden.append("a")),
        trabajador.Etapa("b", b_rota),
    ])
    i = recibir(cliente)
    ahora = ahora_tras_recibir()
    procesar_pendientes(ruta_db, ahora)
    assert trabajo_de(con, i)["etapa"] == "a"
    monkeypatch.setattr(trabajador, "ETAPAS", [
        trabajador.Etapa("a", lambda *_: orden.append("a")),
        trabajador.Etapa("b", lambda *_: orden.append("b")),
    ])
    procesar_pendientes(ruta_db, ahora + timedelta(seconds=30))
    assert orden == ["a", "b", "b"]  # "a" no se repite
    assert trabajo_de(con, i)["estado"] == "hecho"


# --- Fallos y reintentos --------------------------------------------------------

def test_fallo_reintenta_con_espera_y_agota_en_fallido(cliente, con, ruta_db, etapa_rota, caplog):
    i = recibir(cliente)
    t0 = ahora_tras_recibir()

    procesar_pendientes(ruta_db, t0)
    t = trabajo_de(con, i)
    assert t["estado"] == "pendiente" and t["intentos"] == 1
    assert leer_utc(t["proximo_intento_at"]) == t0 + timedelta(seconds=30)
    assert t["ultimo_error"] == "RuntimeError"

    # Antes de la hora de reintento no se reclama.
    assert reclamar_trabajo(con, t0 + timedelta(seconds=29)) is None

    esperas = [30]
    momento = t0 + timedelta(seconds=30)
    for _ in range(4):
        procesar_pendientes(ruta_db, momento)
        t = trabajo_de(con, i)
        if t["estado"] == "pendiente":
            siguiente = leer_utc(t["proximo_intento_at"])
            esperas.append(int((siguiente - momento).total_seconds()))
            momento = siguiente
    assert esperas == [30, 120, 600, 1800]
    assert list(ESPERAS_REINTENTO_S) == [30, 120, 600, 1800]

    t = trabajo_de(con, i)
    assert t["estado"] == "fallido" and t["intentos"] == 5
    r = cliente.get("/leads/estado", params={"event_id": evento(con, i)["event_id"]})
    assert r.json()["estado"] == "fallido"
    assert r.json()["motivo"] == "Procesamiento fallido tras 5 intentos (RuntimeError)"
    texto = r.text.lower()
    assert "carla" not in texto and "0991234567" not in texto
    # Ni en la base ni en los logs queda el mensaje de la excepción.
    assert "carla" not in (t["ultimo_error"] or "").lower()
    assert "RuntimeError" in caplog.text and "carla" not in caplog.text.lower()


def test_espera_se_repite_mas_alla_de_la_lista(cliente, con, ruta_db, etapa_rota):
    i = recibir(cliente)
    con.execute("UPDATE trabajos SET max_intentos = 7 WHERE evento_id = ?", (i,))
    momento = ahora_tras_recibir()
    esperas = []
    for _ in range(6):
        procesar_pendientes(ruta_db, momento)
        siguiente = leer_utc(trabajo_de(con, i)["proximo_intento_at"])
        esperas.append(int((siguiente - momento).total_seconds()))
        momento = siguiente
    assert esperas == [30, 120, 600, 1800, 1800, 1800]


@pytest.mark.parametrize("estado", ["rechazado", "duplicado"])
def test_no_pisa_rechazado_ni_duplicado(cliente, con, ruta_db, monkeypatch, estado):
    i = recibir(cliente)
    con.execute("UPDATE eventos SET estado = ?, motivo = 'motivo original' WHERE id = ?", (estado, i))
    con.execute("UPDATE trabajos SET max_intentos = 1 WHERE evento_id = ?", (i,))
    monkeypatch.setattr(procesador, "procesar_evento",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    procesar_pendientes(ruta_db, ahora_tras_recibir())
    assert trabajo_de(con, i)["estado"] == "fallido"
    assert evento(con, i)["estado"] == estado
    assert evento(con, i)["motivo"] == "motivo original"


# --- Recuperación de leases --------------------------------------------------------

def test_lease_vencida_vuelve_a_pendiente_y_se_procesa(cliente, con, ruta_db):
    i = recibir(cliente)
    t0 = ahora_tras_recibir()
    reclamar_trabajo(con, t0, lease_s=60)  # simula un proceso que murió con el trabajo

    # Lease vigente: ni se recupera ni se reclama.
    assert recuperar_expirados(con, t0 + timedelta(seconds=59)) == 0
    assert reclamar_trabajo(con, t0 + timedelta(seconds=59)) is None
    assert trabajo_de(con, i)["estado"] == "en_curso"

    assert procesar_pendientes(ruta_db, t0 + timedelta(seconds=61)) == 1
    t = trabajo_de(con, i)
    assert t["estado"] == "hecho" and t["intentos"] == 2
    assert evento(con, i)["lead_id"] is not None


def test_lease_vencida_en_el_ultimo_intento_queda_fallido(cliente, con, ruta_db):
    i = recibir(cliente)
    con.execute("UPDATE trabajos SET max_intentos = 1 WHERE evento_id = ?", (i,))
    t0 = ahora_tras_recibir()
    reclamar_trabajo(con, t0, lease_s=60)
    recuperar_expirados(con, t0 + timedelta(seconds=61))
    assert trabajo_de(con, i)["estado"] == "fallido"
    assert evento(con, i)["motivo"] == "Procesamiento fallido tras 1 intentos (LeaseVencido)"


def test_dueno_perdido_no_escribe(cliente, con, ruta_db):
    """Si la lease venció y otro reclamó el trabajo, el primero no puede cerrarlo."""
    recibir(cliente)
    t0 = ahora_tras_recibir()
    viejo = reclamar_trabajo(con, t0, lease_s=60)
    recuperar_expirados(con, t0 + timedelta(seconds=61))
    nuevo = reclamar_trabajo(con, t0 + timedelta(seconds=61))
    assert ejecutar_trabajo(con, viejo, t0 + timedelta(seconds=62)) == "perdido"
    assert trabajo_de(con, viejo["evento_id"])["estado"] == "en_curso"
    assert ejecutar_trabajo(con, nuevo, t0 + timedelta(seconds=62)) == "hecho"


# --- Hilo real ------------------------------------------------------------------

def test_reinicio_real_procesa_lo_pendiente(ruta_db, monkeypatch):
    with TestClient(create_app(ruta_db)) as c1:  # trabajador apagado
        assert c1.app.state.trabajador is None
        for _ in range(3):
            recibir(c1)
    assert not sin_trabajos_abiertos(ruta_db)

    monkeypatch.setenv("WORKER_ACTIVO", "1")
    with TestClient(create_app(ruta_db)) as c2:
        hilo = c2.app.state.trabajador
        assert hilo is not None and hilo.vivo()
        assert esperar(lambda: sin_trabajos_abiertos(ruta_db)), "no se procesó en 5 s"
    assert not hilo.vivo()  # se detiene limpiamente al apagar
    with sqlite3.connect(ruta_db) as c:
        assert c.execute("SELECT COUNT(*) FROM trabajos WHERE estado = 'hecho'").fetchone()[0] == 3


def test_error_inesperado_del_bucle_no_mata_el_hilo(ruta_db, monkeypatch, caplog):
    real = trabajador.procesar_pendientes
    llamadas = {"n": 0}

    def a_veces_falla(*a, **k):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(trabajador, "procesar_pendientes", a_veces_falla)
    monkeypatch.setenv("WORKER_ACTIVO", "1")
    with TestClient(create_app(ruta_db)) as c:
        recibir(c)
        assert esperar(lambda: sin_trabajos_abiertos(ruta_db))
        assert c.app.state.trabajador.vivo()
    assert "error en el bucle del trabajador: OperationalError" in caplog.text


def test_diez_payloads_con_el_trabajador_real(ruta_db, monkeypatch):
    monkeypatch.setenv("WORKER_ACTIVO", "1")
    with TestClient(create_app(ruta_db)) as c:
        ids = {f.stem[-2:]: recibir(c, f.read_bytes()) for f in sorted(PAYLOADS.glob("evento_*.json"))}
        assert esperar(lambda: sin_trabajos_abiertos(ruta_db)), "quedaron trabajos sin procesar"

    con = db.conectar(ruta_db)
    try:
        def ev(n):
            return evento(con, ids[n])

        assert ids["01"] == ids["02"]
        assert con.execute("SELECT COUNT(*) FROM trabajos WHERE estado = 'hecho'").fetchone()[0] == 9
        assert ev("01")["telefono_e164"] == "+593991234567"
        assert ev("03")["estado"] == "duplicado"
        assert ev("03")["duplicado_de"] == "evt_01JB7Q3M2X4K8R9T5W6Y"
        assert ev("03")["lead_id"] == ev("01")["lead_id"]
        assert ev("04")["telefono_e164"] == "+13055550142"
        assert ev("05")["telefono_e164"] is None
        assert ev("05")["email"] == "lectora.anonima@example.com"
        assert ev("06")["telefono_e164"] == "+593987650021"
        assert con.execute("SELECT ciudad FROM leads WHERE evento_id = ?",
                           (ids["06"],)).fetchone()[0] == "Quito"
        assert ev("07")["telefono_e164"] == "+593987654321"
        assert ev("08")["telefono_e164"] is None and ev("08")["email"] == "promo@ganadinero.example"
        for n in ("01", "04", "05", "06", "07", "08", "09", "10"):
            assert ev(n)["estado"] == "pendiente" and ev(n)["lead_id"] is not None, n
        assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 8
        assert con.execute("SELECT COUNT(*) FROM lead_mensajes").fetchone()[0] == 1
    finally:
        con.close()
