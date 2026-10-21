"""Paso 3 (5b, D19): manejador central de errores con bandeja propia (incidencias)."""
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import config, db, despachador, errores, procesador, trabajador
from app.canales import CanalArchivo, CanalN8n, ErrorCanal
from app.errores import reportar_error

N8N_ERRORES = "https://n8n.example/webhook/errores-ruta-secreta"


@pytest.fixture
def base(tmp_path):
    ruta = tmp_path / "leads.db"
    db.inicializar(ruta)
    errores.configurar(ruta, ventana_s=900)
    yield ruta
    errores.desconfigurar()


def filas(ruta):
    c = sqlite3.connect(ruta)
    c.row_factory = sqlite3.Row
    try:
        return c.execute("SELECT * FROM incidencias ORDER BY id").fetchall()
    finally:
        c.close()


def txt(momento):
    return momento.astimezone(timezone.utc).strftime(db.FORMATO_UTC)


# --- Registro y deduplicación ----------------------------------------------------------------------

def test_escribe_una_fila_y_deduplica_en_la_ventana(base):
    reportar_error("llm", "timeout", evento_id=1)
    reportar_error("llm", "timeout", evento_id=2)
    reportar_error("api_pacientes", "timeout", evento_id=3)   # otra clave (origen)
    reportar_error("llm", "credenciales", evento_id=4)        # otro código
    f = filas(base)
    assert [(x["codigo"], x["clave"], x["ocurrencias"], x["estado"]) for x in f] == [
        ("timeout", "llm", 2, "pendiente"), ("timeout", "api_pacientes", 1, "pendiente"),
        ("credenciales", "llm", 1, "pendiente")]


def test_ya_enviada_dentro_de_la_ventana_solo_cuenta(base):
    reportar_error("avisos", "aviso_fallido", evento_id=1)
    c = sqlite3.connect(base)
    c.execute("UPDATE incidencias SET estado = 'enviado', enviado_at = creado_at")
    c.commit()
    reportar_error("avisos", "aviso_fallido", evento_id=2)
    [f] = filas(base)
    assert f["ocurrencias"] == 2 and f["estado"] == "enviado"


def test_fuera_de_la_ventana_crea_otra(base):
    reportar_error("avisos", "aviso_fallido", evento_id=1)
    viejo = txt(datetime.now(timezone.utc) - timedelta(seconds=1000))
    c = sqlite3.connect(base)
    c.execute("UPDATE incidencias SET estado = 'enviado', enviado_at = ?, creado_at = ?", (viejo, viejo))
    c.commit()
    reportar_error("avisos", "aviso_fallido", evento_id=2)
    assert [x["estado"] for x in filas(base)] == ["enviado", "pendiente"]


def test_unica_pendiente_por_codigo_y_clave_en_la_base(base):
    reportar_error("llm", "timeout")
    c = sqlite3.connect(base)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("""INSERT INTO incidencias (codigo, clave, detalle, ocurrencias, creado_at, ultima_at,
                                              estado, intentos, proximo_intento)
                     VALUES ('timeout', 'llm', '', 1, '2026-01-01T00:00:00.000000Z',
                             '2026-01-01T00:00:00.000000Z', 'pendiente', 0, '2026-01-01T00:00:00.000000Z')""")


def test_detalle_solo_codigos_contadores_e_ids(base):
    reportar_error("vigilancia", "pendiente_antiguo", datos={"conteo": 3, "antiguedad_max_s": 1200})
    reportar_error("llm", "timeout", evento_id=12)
    f = filas(base)
    assert f[0]["detalle"] == "origen=vigilancia conteo=3 antiguedad_max_s=1200"
    assert f[1]["detalle"] == "origen=llm evento=12"


# --- Nunca lanza ni bloquea --------------------------------------------------------------------------

@pytest.mark.parametrize("ruta_rota", ["inexistente/sub/x.db", "es_un_directorio", "no_es_sqlite.db"])
def test_no_lanza_con_la_base_rota(tmp_path, caplog, ruta_rota):
    ruta = tmp_path / ruta_rota
    if ruta_rota == "es_un_directorio":
        ruta.mkdir()
    if ruta_rota == "no_es_sqlite.db":
        ruta.write_bytes(b"esto no es una base sqlite" * 100)
    errores.configurar(ruta, ventana_s=900)
    try:
        reportar_error("llm", "timeout", evento_id=1)  # no lanza
    finally:
        errores.desconfigurar()
    assert "no se pudo registrar la incidencia" in caplog.text


def test_mensaje_absurdo_no_lanza_y_se_sanea(base):
    reportar_error(None, "a\nb" * 1000, evento_id="Carla Mendoza 0991234567", nivel=12345,
                   datos={"conteo": "texto libre Carla", "x" * 300: 1, "ok": 2}, clave="‮" * 500)
    reportar_error("llm\nRuta: basura", "timeout", datos=["no", "es", "dict"])
    for f in filas(base):
        assert len(f["detalle"]) <= 200 and len(f["codigo"]) <= 40 and len(f["clave"]) <= 40
        for campo in (f["codigo"], f["clave"], f["detalle"]):
            assert "\n" not in campo and "Carla" not in campo and "099" not in campo and "‮" not in campo
    assert filas(base)[0]["codigo"] == "codigo_invalido"


def test_no_bloquea_con_la_base_ocupada(base):
    otra = sqlite3.connect(base, isolation_level=None)
    otra.execute("BEGIN IMMEDIATE")  # alguien retiene el bloqueo de escritura
    try:
        inicio = time.monotonic()
        reportar_error("llm", "timeout")
        assert time.monotonic() - inicio < 2.5
    finally:
        otra.execute("ROLLBACK")
        otra.close()


def test_sin_configurar_solo_registra(tmp_path, caplog):
    errores.desconfigurar()
    caplog.set_level(logging.WARNING)
    reportar_error("llm", "timeout", evento_id=1)
    assert "codigo=timeout" in caplog.text
    assert not list(tmp_path.iterdir())


def test_evento_fallido_al_agotar_intentos(base, monkeypatch):
    c = db.conectar(base)
    db.registrar_recepcion(c, cuerpo=b'{"event_type": "lead.created", "lead": {"phone": "0991234567"}}',
                           event_id="evt_x", created_at_origen=None, estado="pendiente", motivo=None, max_intentos=1)
    c.close()
    monkeypatch.setattr(procesador, "procesar_evento", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Carla")))
    trabajador.procesar_pendientes(base, datetime.now(timezone.utc) + timedelta(seconds=1))
    [f] = [x for x in filas(base) if x["codigo"] == "evento_fallido"]
    assert f["detalle"] == "origen=trabajador evento=1" and "Carla" not in f["detalle"]


# --- Envío de incidencias ------------------------------------------------------------------------------

class Falla:
    nombre = "falla"

    def enviar(self, texto, registro, presupuesto=None):
        raise ErrorCanal("temporal", "caido")


def _una_incidencia(base):
    reportar_error("avisos", "aviso_fallido", evento_id=7)
    return datetime.strptime(filas(base)[0]["proximo_intento"], db.FORMATO_UTC).replace(tzinfo=timezone.utc)


def test_envio_por_archivo(base, tmp_path):
    t0 = _una_incidencia(base)
    archivo = tmp_path / "errores.jsonl"
    assert despachador.despachar_incidencias(base, CanalArchivo(archivo), t0) == 1
    [linea] = [json.loads(x) for x in archivo.read_text().splitlines()]
    assert linea["codigo"] == "aviso_fallido" and linea["ocurrencias"] == 1
    assert "aviso_fallido" in linea["texto"] and filas(base)[0]["estado"] == "enviado"


def test_reintentos_exactos_y_fallido_tras_5(base, caplog):
    t = _una_incidencia(base)
    esperas = []
    for intento in range(1, 6):
        despachador.despachar_incidencias(base, Falla(), t)
        f = filas(base)[0]
        assert f["intentos"] == intento and f["ultimo_error"] == "caido"
        if f["estado"] == "pendiente":
            siguiente = datetime.strptime(f["proximo_intento"], db.FORMATO_UTC).replace(tzinfo=timezone.utc)
            esperas.append(round((siguiente - t).total_seconds()))
            t = siguiente
    assert esperas == [10, 60, 300, 900]
    assert filas(base)[0]["estado"] == "fallido"
    # Un fallo al enviar incidencias no genera otra incidencia (sin bucles).
    assert len(filas(base)) == 1


def test_retoma_tras_reinicio(base):
    t0 = _una_incidencia(base)
    c = db.conectar(base)
    try:
        assert despachador.reclamar_incidencia(c, t0) is not None  # el proceso "muere"
    finally:
        c.close()

    class Ok:
        nombre = "ok"
        enviados = []

        def enviar(self, texto, registro, presupuesto=None):
            self.enviados.append(texto)

    ok = Ok()
    assert despachador.despachar_incidencias(base, ok, t0 + timedelta(seconds=10)) == 0  # lease vigente
    assert despachador.despachar_incidencias(base, ok, t0 + timedelta(seconds=despachador.LEASE_S + 1)) == 1
    assert filas(base)[0]["estado"] == "enviado" and filas(base)[0]["intentos"] == 2


def test_n8n_errores_contrato_y_secretos_fuera_de_logs(base, caplog):
    caplog.set_level(logging.DEBUG)
    t0 = _una_incidencia(base)
    vistas = []

    def servidor(req):
        vistas.append(req)
        return httpx.Response(500)

    canal = CanalN8n(N8N_ERRORES, "secreto-xyz", transport=httpx.MockTransport(servidor), formato="error")
    despachador.despachar_incidencias(base, canal, t0)
    cuerpo = json.loads(vistas[0].content)
    assert set(cuerpo) == {"codigo", "detalle", "ocurrencias", "ts"}
    assert cuerpo["codigo"] == "aviso_fallido" and cuerpo["ocurrencias"] == 1
    assert N8N_ERRORES not in caplog.text and "ruta-secreta" not in caplog.text and "secreto-xyz" not in caplog.text


def test_despachador_drena_avisos_e_incidencias(base, tmp_path):
    t0 = _una_incidencia(base)
    archivo = tmp_path / "errores.jsonl"
    despachador.despachar_pendientes(base, CanalArchivo(tmp_path / "avisos.jsonl"), t0,
                                     canal_errores=CanalArchivo(archivo))
    assert filas(base)[0]["estado"] == "enviado" and archivo.exists()


def test_configuracion_de_errores_por_defecto(monkeypatch):
    monkeypatch.delenv("ERRORES_ARCHIVO")
    cfg = config.cargar_config()
    assert cfg.errores_canales == ("archivo",) and cfg.errores_ventana_s == 900
    assert cfg.errores_archivo == config.BASE_DIR / "data" / "errores.jsonl"
