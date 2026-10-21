"""Bloque 5b.1 (D18 corregida): el archivo es auditoría, no un canal de entrega.

Con algún notificador (telegram o n8n) configurado, un aviso o una incidencia solo es
'enviado' cuando un notificador lo acepta. El archivo se escribe una vez por registro.
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db, despachador, errores
from app.canales import CanalArchivo, CanalCompuesto, CanalN8n, CanalTelegram
from app.main import create_app

from test_avisos import (  # noqa: F401  (fixtures y helpers compartidos)
    avisos, cliente, con, evento, lead, leer, procesar, recibir, ruta_db,
)

N8N_URL = "https://n8n.example/webhook/avisos-ruta-secreta"
TOKEN = "123456789:AAH-token_secreto_de_prueba_xyz"


class Servidor:
    """Responde con la lista dada (la última se repite) y cuenta las peticiones."""
    def __init__(self, respuestas):
        self.respuestas, self.peticiones = list(respuestas), []

    def __call__(self, request):
        self.peticiones.append(request)
        r = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(r, Exception):
            raise r
        return r


def n8n(respuestas, formato="aviso"):
    s = Servidor(respuestas)
    return CanalN8n(N8N_URL, "secreto", transport=httpx.MockTransport(s), formato=formato), s


def telegram(respuestas):
    s = Servidor(respuestas)
    return CanalTelegram(TOKEN, "-100123", transport=httpx.MockTransport(s)), s


OK_TG = httpx.Response(200, json={"ok": True})


def _un_caliente(cliente, ruta_db, con):
    i = recibir(cliente, lead(mensaje="Quiero agendar una cita"))
    procesar(ruta_db)
    return i, leer(avisos(con, i)[0]["proximo_intento"])


def lineas(ruta):
    return ruta.read_text(encoding="utf-8").splitlines() if ruta.exists() else []


# --- n8n 500 + archivo ok ----------------------------------------------------------------------------

def test_n8n_falla_y_el_archivo_no_cuenta_como_entrega(cliente, con, ruta_db, tmp_path):
    i, t = _un_caliente(cliente, ruta_db, con)
    archivo = tmp_path / "auditoria.jsonl"
    canal_n8n, s = n8n([httpx.Response(500), httpx.Response(500), httpx.Response(200)])
    canal = CanalCompuesto([canal_n8n, CanalArchivo(archivo)])

    despachador.despachar_pendientes(ruta_db, canal, t)
    a = avisos(con, i)[0]
    assert a["estado"] == "pendiente" and a["intentos"] == 1 and a["archivado"] == 1
    assert len(lineas(archivo)) == 1

    t2 = leer(a["proximo_intento"])
    assert (t2 - t).total_seconds() == 5
    despachador.despachar_pendientes(ruta_db, canal, t2)
    a = avisos(con, i)[0]
    assert a["estado"] == "pendiente" and a["intentos"] == 2
    t3 = leer(a["proximo_intento"])
    assert (t3 - t2).total_seconds() == 15

    despachador.despachar_pendientes(ruta_db, canal, t3)
    a = avisos(con, i)[0]
    assert a["estado"] == "enviado" and a["intentos"] == 3
    assert len(s.peticiones) == 3
    assert len(lineas(archivo)) == 1  # una sola línea pese a los 3 intentos


def test_vencido_aunque_el_archivo_este_bien(cliente, con, ruta_db, tmp_path, caplog):
    i, t = _un_caliente(cliente, ruta_db, con)
    recibido = leer(evento(con, i)["received_at"])
    canal_n8n, _ = n8n([httpx.Response(503)])
    canal = CanalCompuesto([canal_n8n, CanalArchivo(tmp_path / "a.jsonl")])
    despachador.despachar_pendientes(ruta_db, canal, t)
    con.execute("UPDATE avisos SET proximo_intento = ?", (despachador._txt(recibido),))
    despachador.despachar_pendientes(ruta_db, canal, recibido + timedelta(seconds=301))
    a = avisos(con, i)[0]
    assert a["estado"] == "pendiente" and a["vencido_notificado"] == 1
    assert "codigo=aviso_vencido" in caplog.text


# --- Varios notificadores ------------------------------------------------------------------------------

def test_telegram_y_n8n_basta_uno(cliente, con, ruta_db, tmp_path):
    i, t = _un_caliente(cliente, ruta_db, con)
    tg, s_tg = telegram([httpx.Response(500)])
    nn, s_nn = n8n([httpx.Response(200)])
    despachador.despachar_pendientes(ruta_db, CanalCompuesto([tg, nn, CanalArchivo(tmp_path / "a.jsonl")]), t)
    assert avisos(con, i)[0]["estado"] == "enviado"


def test_telegram_y_n8n_caidos_reintenta(cliente, con, ruta_db, tmp_path):
    i, t = _un_caliente(cliente, ruta_db, con)
    tg, _ = telegram([httpx.ConnectError("x")])
    nn, _ = n8n([httpx.Response(502)])
    despachador.despachar_pendientes(ruta_db, CanalCompuesto([tg, nn, CanalArchivo(tmp_path / "a.jsonl")]), t)
    a = avisos(con, i)[0]
    assert a["estado"] == "pendiente" and (leer(a["proximo_intento"]) - t).total_seconds() == 5


def test_todos_los_notificadores_permanentes_es_fallido(cliente, con, ruta_db, tmp_path):
    i, t = _un_caliente(cliente, ruta_db, con)
    tg, _ = telegram([httpx.Response(401)])
    nn, _ = n8n([httpx.Response(403)])
    despachador.despachar_pendientes(ruta_db, CanalCompuesto([tg, nn, CanalArchivo(tmp_path / "a.jsonl")]), t)
    assert avisos(con, i)[0]["estado"] == "fallido"


# --- Solo archivo y archivo roto -------------------------------------------------------------------------

def test_solo_archivo_si_cuenta_como_entrega(cliente, con, ruta_db, tmp_path):
    i, t = _un_caliente(cliente, ruta_db, con)
    archivo = tmp_path / "a.jsonl"
    despachador.despachar_pendientes(ruta_db, CanalCompuesto([CanalArchivo(archivo)]), t)
    a = avisos(con, i)[0]
    assert a["estado"] == "enviado" and a["archivado"] == 1 and len(lineas(archivo)) == 1


def test_archivo_no_escribible_no_bloquea_el_envio(cliente, con, ruta_db, tmp_path, caplog):
    i, t = _un_caliente(cliente, ruta_db, con)
    bloqueo = tmp_path / "no_es_dir"
    bloqueo.write_text("")
    nn, _ = n8n([httpx.Response(200)])
    despachador.despachar_pendientes(ruta_db, CanalCompuesto([nn, CanalArchivo(bloqueo / "a.jsonl")]), t)
    a = avisos(con, i)[0]
    assert a["estado"] == "enviado" and a["archivado"] == 0
    assert "archivo_no_escribible" in caplog.text


# --- Incidencias con la misma regla -----------------------------------------------------------------------

def test_incidencias_el_archivo_es_auditoria(ruta_db, cliente, tmp_path):
    errores.configurar(ruta_db, 900)
    errores.reportar_error("avisos", "aviso_fallido", evento_id=1)
    errores.desconfigurar()
    c = sqlite3.connect(ruta_db)
    c.row_factory = sqlite3.Row
    t = datetime.strptime(c.execute("SELECT proximo_intento FROM incidencias").fetchone()[0],
                          db.FORMATO_UTC).replace(tzinfo=timezone.utc)
    archivo = tmp_path / "errores.jsonl"
    nn, s = n8n([httpx.Response(500), httpx.Response(200)], formato="error")
    canal = CanalCompuesto([nn, CanalArchivo(archivo)])
    despachador.despachar_incidencias(ruta_db, canal, t)
    f = c.execute("SELECT * FROM incidencias").fetchone()
    assert f["estado"] == "pendiente" and f["archivado"] == 1 and len(lineas(archivo)) == 1
    despachador.despachar_incidencias(ruta_db, canal, t + timedelta(seconds=10))
    f = c.execute("SELECT * FROM incidencias").fetchone()
    assert f["estado"] == "enviado" and len(s.peticiones) == 2 and len(lineas(archivo)) == 1
    c.close()


def test_incidencias_solo_archivo_si_cuenta(ruta_db, cliente, tmp_path):
    errores.configurar(ruta_db, 900)
    errores.reportar_error("avisos", "aviso_fallido", evento_id=1)
    errores.desconfigurar()
    c = sqlite3.connect(ruta_db)
    t = datetime.strptime(c.execute("SELECT proximo_intento FROM incidencias").fetchone()[0],
                          db.FORMATO_UTC).replace(tzinfo=timezone.utc)
    despachador.despachar_incidencias(ruta_db, CanalCompuesto([CanalArchivo(tmp_path / "e.jsonl")]), t)
    assert c.execute("SELECT estado FROM incidencias").fetchone()[0] == "enviado"
    c.close()


# --- Aviso al arrancar ------------------------------------------------------------------------------------

def test_aviso_sin_canal_de_notificacion(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    create_app(tmp_path / "leads.db")
    mensajes = [r for r in caplog.records
                if "sin canal de notificación: los avisos solo se guardan en archivo" in r.getMessage()]
    assert len(mensajes) == 1 and mensajes[0].levelno == logging.WARNING


def test_con_notificador_no_avisa(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("AVISOS_CANAL", "n8n,archivo")
    monkeypatch.setenv("N8N_AVISOS_URL", N8N_URL)
    caplog.set_level(logging.WARNING)
    create_app(tmp_path / "leads.db")
    assert "sin canal de notificación" not in caplog.text
