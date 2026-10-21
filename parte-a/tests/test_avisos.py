"""Bloque 5a: enrutado transaccional, bandeja de avisos, despachador y canal archivo."""
import json
import logging
import os
import sqlite3
import stat
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db, despachador, etapa_enrutar, trabajador
from app.despachador import (
    CanalArchivo, Despachador, ErrorCanal, despachar_pendientes, reclamar_aviso, revisar_vencidos,
)
from app.main import create_app
from app.trabajador import Contexto, procesar_pendientes

PAYLOADS = Path(__file__).resolve().parents[2] / "payloads"
URL = "/webhook/leads"
MENSAJE_PRIVADO = "Mensaje privadísimo XYZ sobre mi caso"


# --- Dobles y helpers --------------------------------------------------------------------------

class CanalFalso:
    nombre = "falso"

    def __init__(self, fallos=None):
        self.fallos = list(fallos or [])  # excepciones a lanzar, en orden
        self.enviados: list[tuple[str, int]] = []

    def enviar(self, texto, aviso, presupuesto=None):
        if self.fallos:
            raise self.fallos.pop(0)
        self.enviados.append((texto, aviso["id"]))


class FallaSiempre(CanalFalso):
    def enviar(self, texto, aviso, presupuesto=None):
        raise ErrorCanal("temporal", "caido")


@pytest.fixture
def ruta_db(tmp_path):
    return tmp_path / "leads.db"


@pytest.fixture
def cliente(ruta_db):
    with TestClient(create_app(ruta_db)) as c:
        yield c


@pytest.fixture
def con(ruta_db, cliente):
    c = db.conectar(ruta_db)
    yield c
    c.close()


def lead(mensaje="Hola, quisiera información", nombre="Carla Mendoza", phone="0991234567",
         email="carla.mendoza@example.com", servicio="Limpieza"):
    return json.dumps({"event_id": f"evt_{uuid.uuid4().hex}", "event_type": "lead.created",
                       "source": {"platform": "instagram", "campaign": "Sonrisa Blanca"},
                       "lead": {"full_name": nombre, "phone": phone, "email": email,
                                "service_interest": servicio, "message": mensaje}}).encode()


def recibir(cliente, cuerpo) -> int:
    r = cliente.post(URL, content=cuerpo)
    assert r.status_code in (200, 202)
    with sqlite3.connect(cliente.app.state.db_path) as c:
        return c.execute("SELECT id FROM eventos WHERE recepcion_id = ?",
                         (r.json()["recepcion_id"],)).fetchone()[0]


def ahora():
    return datetime.now(timezone.utc) + timedelta(seconds=1)


def procesar(ruta_db, momento=None, ctx=None):
    return procesar_pendientes(ruta_db, momento or ahora(), ctx=ctx or Contexto(config.cargar_config()))


def evento(con, i):
    return con.execute("SELECT * FROM eventos WHERE id = ?", (i,)).fetchone()


def avisos(con, i=None):
    sql, args = "SELECT * FROM avisos", ()
    if i is not None:
        sql, args = sql + " WHERE evento_id = ?", (i,)
    return con.execute(sql + " ORDER BY id", args).fetchall()


def leer(texto):
    return datetime.strptime(texto, db.FORMATO_UTC).replace(tzinfo=timezone.utc)


# --- Enrutado transaccional (D15) ------------------------------------------------------------------

def test_caliente_tibio_y_basura(cliente, con, ruta_db):
    cal = recibir(cliente, lead(mensaje="Quiero agendar una cita"))
    tib = recibir(cliente, lead(mensaje="¿Cuánto cuesta la limpieza?", nombre="Ana Pérez", phone="0987654321",
                                email="ana@example.com"))
    bas = recibir(cliente, lead(mensaje="Gana dinero desde casa", nombre="Promo", phone="0981112233",
                                email="promo@example.com"))
    procesar(ruta_db)
    for i, ruta in ((cal, "caliente"), (tib, "tibio"), (bas, "basura")):
        e = evento(con, i)
        assert e["estado"] == "procesado" and e["ruta"] == ruta
    [a] = avisos(con)
    assert a["evento_id"] == cal and a["tipo"] == "lead_caliente" and a["estado"] == "pendiente"
    assert leer(a["vence_at"]) == leer(evento(con, cal)["received_at"]) + timedelta(seconds=300)
    citas = con.execute("SELECT * FROM citas_tentativas WHERE evento_id = ?", (cal,)).fetchall()
    assert len(citas) == 2 and all("tentativa" in c["nota"] for c in citas)
    [s] = con.execute("SELECT * FROM seguimientos").fetchall()
    assert s["evento_id"] == tib and s["mensaje"].startswith("Hola Ana") and len(s["mensaje"]) < 500
    assert evento(con, bas)["motivo"]  # basura: motivo registrado, nada más
    assert "texto" not in {r[1] for r in con.execute("PRAGMA table_info(avisos)")}


def test_fallo_antes_del_commit_deja_el_evento_pendiente_y_sin_aviso(cliente, con, ruta_db, monkeypatch):
    original = despachador.crear_aviso

    def explota(*a, **k):
        raise RuntimeError("fallo simulado antes del commit")

    monkeypatch.setattr(despachador, "crear_aviso", explota)
    i = recibir(cliente, lead(mensaje="Quiero agendar una cita"))
    t0 = ahora()
    procesar(ruta_db, t0)
    e = evento(con, i)
    assert e["estado"] == "pendiente" and e["ruta"] is None
    assert avisos(con) == []
    assert con.execute("SELECT COUNT(*) FROM citas_tentativas").fetchone()[0] == 0  # rollback
    t = con.execute("SELECT * FROM trabajos WHERE evento_id = ?", (i,)).fetchone()
    assert t["estado"] == "pendiente" and t["etapa"] == "clasificar_lead"
    monkeypatch.setattr(despachador, "crear_aviso", original)
    procesar(ruta_db, t0 + timedelta(seconds=30))
    assert evento(con, i)["estado"] == "procesado" and len(avisos(con, i)) == 1


def test_repetir_el_enrutado_no_duplica(cliente, con, ruta_db):
    i = recibir(cliente, lead(mensaje="Quiero agendar una cita"))
    j = recibir(cliente, lead(mensaje="info", nombre="Ana Pérez", phone="0987654321", email="ana@example.com"))
    ctx = Contexto(config.cargar_config())
    procesar(ruta_db, ctx=ctx)
    for k in (i, j):
        t = con.execute("SELECT * FROM trabajos WHERE evento_id = ?", (k,)).fetchone()
        etapa_enrutar.enrutar_lead(con, t, ctx)
        etapa_enrutar.enrutar_lead(con, t, ctx)
    assert len(avisos(con)) == 1
    assert con.execute("SELECT COUNT(*) FROM citas_tentativas").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM seguimientos").fetchone()[0] == 1


def test_crear_aviso_es_idempotente(cliente, con, ruta_db):
    i = recibir(cliente, lead(mensaje="Quiero agendar una cita"))
    procesar(ruta_db)
    cfg = config.cargar_config()
    con.execute("BEGIN IMMEDIATE")
    assert despachador.crear_aviso(con, i, "lead_caliente", evento(con, i)["received_at"], cfg, ahora()) is False
    con.execute("COMMIT")
    assert len(avisos(con)) == 1


def test_rechazado_y_duplicado_sin_novedad_no_generan_nada(cliente, con, ruta_db):
    rech = recibir(cliente, lead(phone="1111111111", email=None))
    orig = recibir(cliente, lead(mensaje="Quiero agendar una cita, me duele mucho la muela"))
    dup = recibir(cliente, lead(mensaje="Quiero agendar, me duele mucho"))
    procesar(ruta_db)
    assert evento(con, rech)["estado"] == "rechazado" and evento(con, dup)["estado"] == "duplicado"
    assert [a["evento_id"] for a in avisos(con)] == [orig]
    assert con.execute("SELECT COUNT(*) FROM seguimientos").fetchone()[0] == 0


def test_novedad_de_duplicado_crea_su_aviso(cliente, con, ruta_db, tmp_path):
    recibir(cliente, lead(mensaje="Solo quiero precios"))
    dup = recibir(cliente, lead(mensaje="Tengo sangrado abundante y quiero atención inmediata"))
    procesar(ruta_db)
    e = evento(con, dup)
    assert e["estado"] == "duplicado" and e["ruta"] is None
    [a] = avisos(con, dup)
    assert a["tipo"] == "novedad_duplicado"
    canal = CanalFalso()
    despachar_pendientes(ruta_db, canal, ahora())
    textos = [t for t, _ in canal.enviados]
    assert any("Actualización de un lead existente" in t and "urgencia clínica" in t for t in textos)
    assert all("sangrado abundante" not in t for t in textos)  # nunca el texto libre


# --- Despachador ------------------------------------------------------------------------------------

def _un_caliente(cliente, ruta_db, con) -> tuple[int, datetime]:
    """(evento, instante en que su aviso ya es reclamable)."""
    i = recibir(cliente, lead(mensaje=f"Quiero agendar una cita. {MENSAJE_PRIVADO}"))
    procesar(ruta_db)
    return i, leer(avisos(con, i)[0]["proximo_intento"])


def test_envio_con_latencia(cliente, con, ruta_db):
    i, listo = _un_caliente(cliente, ruta_db, con)
    recibido = leer(evento(con, i)["received_at"])
    canal = CanalFalso()
    assert despachar_pendientes(ruta_db, canal, recibido + timedelta(seconds=12)) == 1
    [a] = avisos(con, i)
    assert a["estado"] == "enviado" and a["intentos"] == 1
    assert a["latencia_s"] == pytest.approx(12, abs=0.01)  # envío - recepción del evento
    [(texto, _)] = canal.enviados
    assert "Carla Mendoza" in texto and "+593991234567" in texto and MENSAJE_PRIVADO not in texto
    assert despachar_pendientes(ruta_db, canal, recibido + timedelta(seconds=20)) == 0  # ya enviado


def test_secuencia_exacta_de_reintentos_y_fallido_tras_10(cliente, con, ruta_db, caplog):
    i, listo = _un_caliente(cliente, ruta_db, con)
    canal = FallaSiempre()
    momento, esperas = listo, []
    for intento in range(1, 11):
        despachar_pendientes(ruta_db, canal, momento)
        a = avisos(con, i)[0]
        assert a["intentos"] == intento and a["ultimo_error"] == "caido"
        if a["estado"] == "pendiente":
            siguiente = leer(a["proximo_intento"])
            esperas.append(round((siguiente - momento).total_seconds()))
            momento = siguiente
    assert esperas == [5, 15, 45, 90, 120, 120, 120, 120, 120]
    assert avisos(con, i)[0]["estado"] == "fallido"
    assert caplog.text.count("codigo=aviso_fallido") == 1


def test_espera_contada_desde_el_fallo(cliente, con, ruta_db):
    i, listo = _un_caliente(cliente, ruta_db, con)
    reloj = {"t": listo}

    class Lento(CanalFalso):
        def enviar(self, texto, aviso, presupuesto=None):
            reloj["t"] += timedelta(seconds=8)
            raise ErrorCanal("temporal", "lento")

    despachar_pendientes(ruta_db, Lento(), lambda: reloj["t"])
    assert leer(avisos(con, i)[0]["proximo_intento"]) == listo + timedelta(seconds=8 + 5)


def test_error_permanente_va_directo_a_fallido(cliente, con, ruta_db):
    i, listo = _un_caliente(cliente, ruta_db, con)
    despachar_pendientes(ruta_db, CanalFalso([ErrorCanal("permanente", "rechazado")]), listo)
    a = avisos(con, i)[0]
    assert a["estado"] == "fallido" and a["intentos"] == 1


def test_excepcion_inesperada_del_canal_cuenta_como_temporal(cliente, con, ruta_db):
    i, listo = _un_caliente(cliente, ruta_db, con)
    despachar_pendientes(ruta_db, CanalFalso([KeyError("Carla Mendoza")]), listo)
    a = avisos(con, i)[0]
    assert a["estado"] == "pendiente" and a["ultimo_error"] == "KeyError"


def test_vencido_se_notifica_una_sola_vez(cliente, con, ruta_db, caplog):
    i, listo = _un_caliente(cliente, ruta_db, con)
    recibido = leer(evento(con, i)["received_at"])
    canal = FallaSiempre()
    despachar_pendientes(ruta_db, canal, recibido + timedelta(seconds=10))
    assert caplog.text.count("codigo=aviso_vencido") == 0
    for segundos in (301, 400, 600):
        con.execute("UPDATE avisos SET proximo_intento = ? WHERE evento_id = ?",
                    (despachador._txt(recibido), i))
        despachar_pendientes(ruta_db, canal, recibido + timedelta(seconds=segundos))
    assert avisos(con, i)[0]["vencido_notificado"] == 1
    assert caplog.text.count("codigo=aviso_vencido") == 1
    # Un aviso enviado tarde sigue enviándose (y no se vuelve a notificar).
    despachar_pendientes(ruta_db, CanalFalso(), recibido + timedelta(seconds=900))
    assert avisos(con, i)[0]["estado"] == "enviado" and caplog.text.count("codigo=aviso_vencido") == 1


def test_retoma_tras_reinicio_por_lease(cliente, con, ruta_db):
    i, listo = _un_caliente(cliente, ruta_db, con)
    t0 = listo
    assert reclamar_aviso(con, t0) is not None  # el proceso "muere" con el aviso reservado
    assert despachar_pendientes(ruta_db, CanalFalso(), t0 + timedelta(seconds=10)) == 0  # lease vigente
    canal = CanalFalso()
    assert despachar_pendientes(ruta_db, canal, t0 + timedelta(seconds=despachador.LEASE_S + 1)) == 1
    a = avisos(con, i)[0]
    assert a["estado"] == "enviado" and a["intentos"] == 2


def test_hilo_envia_y_se_detiene_limpio(ruta_db, cliente, con):
    i, _ = _un_caliente(cliente, ruta_db, con)
    canal = CanalFalso()
    h = Despachador(ruta_db, canal, intervalo_s=0.05)
    h.iniciar()
    limite = time.monotonic() + 5
    while not canal.enviados and time.monotonic() < limite:
        time.sleep(0.02)
    assert canal.enviados and h.detener(timeout=5) and not h.vivo()


def test_excepcion_del_bucle_no_mata_el_hilo(ruta_db, cliente, con, monkeypatch, caplog):
    real = despachador.despachar_pendientes
    llamadas = {"n": 0}

    def a_veces(*a, **k):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(despachador, "ESPERA_TRAS_ERROR_S", 0.05)
    monkeypatch.setattr(despachador, "despachar_pendientes", a_veces)
    i, _ = _un_caliente(cliente, ruta_db, con)
    canal = CanalFalso()
    h = Despachador(ruta_db, canal, intervalo_s=0.05)
    h.iniciar()
    limite = time.monotonic() + 5
    while not canal.enviados and time.monotonic() < limite:
        time.sleep(0.02)
    try:
        assert canal.enviados and h.vivo()
        assert "error en el bucle del despachador: OperationalError" in caplog.text
    finally:
        h.detener(timeout=5)


def test_cierre_con_plazo_vencido_avisa(ruta_db, cliente, con, caplog):
    dentro, soltar = threading.Event(), threading.Event()

    class Bloqueado(CanalFalso):
        def enviar(self, texto, aviso, presupuesto=None):
            dentro.set()
            soltar.wait(10)

    _un_caliente(cliente, ruta_db, con)
    h = Despachador(ruta_db, Bloqueado(), intervalo_s=0.05)
    h.iniciar()
    try:
        assert dentro.wait(5)
        caplog.set_level(logging.INFO, logger="leads.despachador")
        assert h.detener(timeout=0.2) is False
        assert "despachador no terminó a tiempo" in caplog.text and "despachador detenido" not in caplog.text
    finally:
        soltar.set()
        h._hilo.join(5)


# --- Canal archivo ------------------------------------------------------------------------------------

def test_canal_archivo_jsonl_con_permisos_0600(tmp_path):
    ruta = tmp_path / "sub" / "dir" / "avisos.jsonl"
    canal = CanalArchivo(ruta)
    canal.enviar("Texto 1", {"id": 1, "evento_id": 10, "tipo": "lead_caliente"})
    canal.enviar("Texto 2\ncon salto", {"id": 2, "evento_id": 11, "tipo": "novedad_duplicado"})
    lineas = ruta.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["texto"] for x in lineas] == ["Texto 1", "Texto 2\ncon salto"]
    assert {json.loads(x)["aviso_id"] for x in lineas} == {1, 2}
    assert stat.S_IMODE(os.stat(ruta).st_mode) == 0o600


def test_canal_archivo_corrige_permisos_si_ya_existia(tmp_path):
    ruta = tmp_path / "avisos.jsonl"
    ruta.write_text("")
    os.chmod(ruta, 0o644)
    CanalArchivo(ruta).enviar("x", {"id": 1, "evento_id": 1, "tipo": "lead_caliente"})
    assert stat.S_IMODE(os.stat(ruta).st_mode) == 0o600


def test_canal_archivo_no_escribible_es_temporal(tmp_path):
    bloqueo = tmp_path / "no_es_dir"
    bloqueo.write_text("")
    with pytest.raises(ErrorCanal) as e:
        CanalArchivo(bloqueo / "avisos.jsonl").enviar("x", {"id": 1, "evento_id": 1, "tipo": "lead_caliente"})
    assert e.value.tipo == "temporal"


# --- Sin datos personales en los logs -----------------------------------------------------------------

def test_logs_sin_datos_personales(ruta_db, cliente, con, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    i, listo = _un_caliente(cliente, ruta_db, con)
    recibido = leer(evento(con, i)["received_at"])
    recibir(cliente, lead(mensaje=f"¿Precio? {MENSAJE_PRIVADO}", nombre="Ana Pérez", phone="0987654321",
                          email="ana@example.com"))
    procesar(ruta_db)
    despachar_pendientes(ruta_db, CanalFalso([ErrorCanal("temporal", "caido")]), listo)
    despachar_pendientes(ruta_db, CanalArchivo(tmp_path / "a.jsonl"), recibido + timedelta(seconds=400))
    texto = caplog.text + "".join(str(r.args) for r in caplog.records)
    for dato in ("Carla", "Mendoza", "Ana Pérez", "0991234567", "593991234567", "0987654321",
                 "carla.mendoza@example.com", "ana@example.com", MENSAJE_PRIVADO, "Hola Ana"):
        assert dato not in texto, dato
    assert "aviso_vencido" in texto  # sí se registró el vencimiento, sin datos


# --- Configuración ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("nombre, valor", [
    ("AVISOS_CANAL", "telegram"), ("AVISOS_CANAL", "correo"),
    ("AVISO_PLAZO_S", "59"), ("AVISO_PLAZO_S", "3601"), ("AVISO_PLAZO_S", "cinco"),
    ("FERIADOS", "2026-13-01"), ("FERIADOS", "25/12/2026"), ("FERIADOS", "2026-12-25,mañana"),
    ("AGENDA_URL", "ftp://agenda.example"), ("AGENDA_URL", "https://u:p@agenda.example"),
    ("AGENDA_URL", "no es url"),
])
def test_config_avisos_invalida_no_arranca(tmp_path, monkeypatch, nombre, valor):
    monkeypatch.setenv(nombre, valor)
    with pytest.raises(config.ConfigError, match=nombre):
        create_app(tmp_path / "leads.db")


def test_config_avisos_defectos_y_aviso_unico(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("AVISOS_ARCHIVO")
    cfg = config.cargar_config()
    assert (cfg.avisos_canal, cfg.aviso_plazo_s, cfg.feriados, cfg.agenda_url) == ("archivo", 300, (), None)
    assert cfg.avisos_archivo == config.BASE_DIR / "data" / "avisos.jsonl"
    caplog.set_level(logging.WARNING)
    create_app(tmp_path / "leads.db")
    # Bloque 5b.1: el aviso de arranque solo con archivo cambió de texto.
    assert sum("sin canal de notificación: los avisos solo se guardan en archivo" in r.getMessage()
               for r in caplog.records) == 1


def test_feriados_configurados_se_respetan(cliente, con, ruta_db, monkeypatch):
    hoy_ec = (datetime.now(timezone.utc) - timedelta(hours=5)).date()
    feriados = ",".join(str(hoy_ec + timedelta(days=k)) for k in range(4))
    monkeypatch.setenv("FERIADOS", feriados)
    i = recibir(cliente, lead(mensaje="Quiero agendar una cita"))
    procesar(ruta_db, ctx=Contexto(config.cargar_config()))
    for c in con.execute("SELECT inicio FROM citas_tentativas WHERE evento_id = ?", (i,)):
        assert datetime.fromisoformat(c["inicio"]).date() > hoy_ec + timedelta(days=3)


# --- Los 10 payloads con los hilos reales ---------------------------------------------------------------------

def test_diez_payloads_dos_avisos_y_cinco_seguimientos(ruta_db, monkeypatch, tmp_path):
    archivo = tmp_path / "avisos.jsonl"
    monkeypatch.setenv("WORKER_ACTIVO", "1")
    monkeypatch.setenv("AVISOS_ARCHIVO", str(archivo))
    with TestClient(create_app(ruta_db)) as c:
        ids = {f.stem[-2:]: recibir(c, f.read_bytes()) for f in sorted(PAYLOADS.glob("evento_*.json"))}
        limite = time.monotonic() + 10
        while time.monotonic() < limite:
            if archivo.exists() and len(archivo.read_text().splitlines()) >= 2:
                break
            time.sleep(0.05)
    con = db.conectar(ruta_db)
    try:
        por_id = {v: k for k, v in ids.items() if k != "02"}  # 02 es la misma fila que 01
        av = con.execute("SELECT evento_id, estado FROM avisos ORDER BY evento_id").fetchall()
        assert [(por_id[a["evento_id"]], a["estado"]) for a in av] == [("01", "enviado"), ("07", "enviado")]
        seg = sorted(por_id[r[0]] for r in con.execute("SELECT evento_id FROM seguimientos"))
        assert seg == ["04", "05", "06", "09", "10"]
    finally:
        con.close()
    lineas = [json.loads(x) for x in archivo.read_text().splitlines()]
    assert len(lineas) == 2 and stat.S_IMODE(os.stat(archivo).st_mode) == 0o600
