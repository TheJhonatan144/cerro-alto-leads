"""Bloque 2b: cola de trabajos, lease, reintentos, etapas e hilo trabajador."""
import json
import logging
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
    assert t["etapa"] == "enriquecer_paciente"  # última etapa (Bloque 3)
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


# --- Bloque 2b.1 -------------------------------------------------------------------

from app import config  # noqa: E402
from app import main as app_main  # noqa: E402
from app import normalizar  # noqa: E402

PAYLOAD_01 = (PAYLOADS / "evento_01.json").read_bytes()
PAYLOAD_02 = (PAYLOADS / "evento_02.json").read_bytes()
EVT_01 = "evt_01JB7Q3M2X4K8R9T5W6Y"


def _explota(*_a, **_k):
    raise RuntimeError("Carla Mendoza 0991234567")


def recibir_con_extraccion_rota(cliente, cuerpo) -> int:
    """El webhook falla al extraer el event_id y guarda el evento con event_id NULL."""
    with pytest.MonkeyPatch.context() as m:
        m.setattr(app_main, "normalizar_event_id", _explota)
        i = recibir(cliente, cuerpo)
    with sqlite3.connect(cliente.app.state.db_path) as c:
        assert c.execute("SELECT event_id FROM eventos WHERE id = ?", (i,)).fetchone()[0] is None
    return i


class Reloj:
    """Reloj falso que las etapas pueden adelantar para simular que tardan."""
    def __init__(self, inicio: datetime):
        self.t = inicio

    def __call__(self) -> datetime:
        return self.t

    def avanzar(self, segundos: float) -> None:
        self.t += timedelta(seconds=segundos)


# 1) Re-extracción del event_id en el trabajador

def test_2b1_reextrae_event_id_perdido_en_el_webhook(cliente, con, ruta_db):
    i = recibir_con_extraccion_rota(cliente, PAYLOAD_01)
    procesar_pendientes(ruta_db, ahora_tras_recibir())
    assert evento(con, i)["event_id"] == EVT_01
    assert evento(con, i)["lead_id"] is not None
    r = cliente.get("/leads/estado", params={"event_id": EVT_01})
    assert r.status_code == 200


def test_2b1_reextraccion_que_choca_es_reenvio(cliente, con, ruta_db):
    i1 = recibir_con_extraccion_rota(cliente, PAYLOAD_01)
    i2 = recibir(cliente, PAYLOAD_02)  # el conector reenvía: esta vez sí se extrae
    procesar_pendientes(ruta_db, ahora_tras_recibir())
    e1 = evento(con, i1)
    assert e1["event_id"] is None  # el UNIQUE lo tiene i2
    assert e1["estado"] == "duplicado"
    assert e1["duplicado_de"] == EVT_01
    assert "reenvío" in e1["motivo"].lower()
    assert "carla" not in e1["motivo"].lower() and "099" not in e1["motivo"]
    assert evento(con, i2)["estado"] == "pendiente" and evento(con, i2)["lead_id"] is not None
    assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1


def test_2b1_reextraccion_con_error_inesperado_reintenta_y_no_rechaza(cliente, con, ruta_db, monkeypatch):
    i = recibir_con_extraccion_rota(cliente, PAYLOAD_01)
    con.execute("UPDATE trabajos SET max_intentos = 2 WHERE evento_id = ?", (i,))
    monkeypatch.setattr(normalizar, "normalizar_event_id", _explota, raising=False)
    t0 = ahora_tras_recibir()
    procesar_pendientes(ruta_db, t0)
    t = trabajo_de(con, i)
    assert t["estado"] == "pendiente" and t["intentos"] == 1 and t["ultimo_error"] == "RuntimeError"
    assert evento(con, i)["estado"] == "pendiente"
    procesar_pendientes(ruta_db, t0 + timedelta(seconds=30))
    e = evento(con, i)
    assert trabajo_de(con, i)["estado"] == "fallido"
    assert e["estado"] == "fallido"
    assert e["motivo"] == "Procesamiento fallido tras 2 intentos (RuntimeError)"
    assert e["cuerpo_crudo"] == PAYLOAD_01  # se conserva para reprocesar


def test_2b1_cuerpo_sin_event_id_sigue_como_d3(cliente, con, ruta_db):
    i = recibir(cliente, json.dumps({"event_type": "lead.created", "lead": {
        "full_name": "Ana", "phone": "0991112233"}}).encode())
    procesar_pendientes(ruta_db, ahora_tras_recibir())
    assert evento(con, i)["event_id"] is None
    assert evento(con, i)["estado"] == "pendiente" and evento(con, i)["lead_id"] is not None


# 2) Comprobación de dueño antes de cada etapa

@pytest.fixture
def efectos(monkeypatch):
    registro = []
    monkeypatch.setattr(trabajador, "ETAPAS", [
        trabajador.Etapa("a", lambda *_: registro.append("a")),
        trabajador.Etapa("b", lambda *_: registro.append("b")),
    ])
    return registro


def test_2b1_no_ejecuta_etapas_si_otro_reclamo_el_trabajo(cliente, con, efectos):
    recibir(cliente)
    t0 = ahora_tras_recibir()
    viejo = reclamar_trabajo(con, t0, lease_s=60)
    recuperar_expirados(con, t0 + timedelta(seconds=61))
    reclamar_trabajo(con, t0 + timedelta(seconds=61))
    assert ejecutar_trabajo(con, viejo, t0 + timedelta(seconds=62)) == "perdido"
    assert efectos == []


def test_2b1_no_ejecuta_etapas_con_lease_vencida(cliente, con, efectos):
    recibir(cliente)
    t0 = ahora_tras_recibir()
    t = reclamar_trabajo(con, t0, lease_s=60)
    assert ejecutar_trabajo(con, t, t0 + timedelta(seconds=61)) == "perdido"
    assert efectos == []


def test_2b1_lease_vencida_durante_una_etapa_corta_el_recorrido(cliente, con, ruta_db, monkeypatch):
    registro = []
    t0 = ahora_tras_recibir()
    reloj = Reloj(t0)

    def a_lenta(_c, trabajo, _cfg):
        registro.append(("a", trabajo["intentos"]))
        reloj.avanzar(61)  # tarda más que la lease

    monkeypatch.setattr(trabajador, "ETAPAS", [
        trabajador.Etapa("a", a_lenta),
        trabajador.Etapa("b", lambda _c, trabajo, _cfg: registro.append(("b", trabajo["intentos"]))),
    ])
    monkeypatch.setattr(trabajador, "_ahora", reloj)
    recibir(cliente)
    con.execute("UPDATE trabajos SET proximo_intento_at = ?", (t0.strftime(db.FORMATO_UTC),))
    procesar_pendientes(ruta_db)
    # "b" no corre con la lease vencida del intento 1: el trabajo se recupera por lease y
    # "b" corre en el intento 2, sin repetir "a".
    assert registro == [("a", 1), ("b", 2)]


def test_2b1_guardar_etapa_sin_filas_interrumpe(cliente, con, monkeypatch):
    registro = []

    def a_y_pierde(c, trabajo, _cfg):
        registro.append("a")
        c.execute("UPDATE trabajos SET intentos = intentos + 1 WHERE id = ?", (trabajo["id"],))

    monkeypatch.setattr(trabajador, "ETAPAS", [
        trabajador.Etapa("a", a_y_pierde),
        trabajador.Etapa("b", lambda *_: registro.append("b")),
    ])
    recibir(cliente)
    t0 = ahora_tras_recibir()
    t = reclamar_trabajo(con, t0)
    assert ejecutar_trabajo(con, t, t0) == "perdido"
    assert registro == ["a"]


# 3) Un error en la espera del bucle no mata el hilo

def test_2b1_error_en_la_espera_no_mata_el_hilo(ruta_db):
    db.inicializar(ruta_db)
    cfg = config.Config(db_path=ruta_db, duplicado_ventana_dias=7, worker_activo=True,
                        worker_intervalo_s=10 ** 20, trabajo_lease_s=60, trabajo_max_intentos=5)
    h = trabajador.Trabajador(ruta_db, cfg)
    h.iniciar()
    try:
        time.sleep(0.3)
        assert h.vivo()
    finally:
        h.detener(timeout=5)


# 4) Cierre con plazo

def test_2b1_cierre_sin_terminar_a_tiempo_avisa(ruta_db, cliente, monkeypatch, caplog):
    dentro, soltar = threading.Event(), threading.Event()

    def bloqueada(*_):
        dentro.set()
        soltar.wait(10)

    monkeypatch.setattr(trabajador, "ETAPAS", [trabajador.Etapa("bloqueada", bloqueada)])
    recibir(cliente)
    h = trabajador.Trabajador(ruta_db, config.cargar_config())
    h.iniciar()
    try:
        assert dentro.wait(5)
        caplog.clear()
        caplog.set_level(logging.INFO, logger="leads.trabajador")
        h.detener(timeout=0.2)
        assert h.vivo()
        assert "trabajador no terminó a tiempo; se recuperará por lease" in caplog.text
        assert "trabajador detenido" not in caplog.text
    finally:
        soltar.set()
        h._hilo.join(5)


# 5) El reintento se programa desde la hora del fallo

def test_2b1_reintento_desde_la_hora_del_fallo(cliente, con, ruta_db, monkeypatch):
    t0 = ahora_tras_recibir()
    reloj = Reloj(t0)

    def lenta_y_falla(*_):
        reloj.avanzar(40)  # tarda más que la primera espera (30 s)
        raise RuntimeError("x")

    monkeypatch.setattr(trabajador, "ETAPAS", [trabajador.Etapa("lenta", lenta_y_falla)])
    monkeypatch.setattr(trabajador, "_ahora", reloj)
    i = recibir(cliente)
    procesar_pendientes(ruta_db)
    assert leer_utc(trabajo_de(con, i)["proximo_intento_at"]) == t0 + timedelta(seconds=40 + 30)
