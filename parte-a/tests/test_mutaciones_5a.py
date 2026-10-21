"""Paso 1 (5b): tests añadidos porque las mutaciones b2, b3, f2 sobrevivían (y h1 solo se
detectaba de forma indirecta)."""
import sqlite3
import threading
from datetime import timedelta

from test_avisos import (  # noqa: F401  (fixtures y helpers compartidos)
    ahora, avisos, cliente, con, evento, lead, procesar, recibir, ruta_db,
)
from app import config, db, despachador, etapa_enrutar
from app.despachador import reclamar_aviso
from app.trabajador import Contexto


def _reabrir(con, i):
    """Devuelve un evento procesado a 'pendiente' para forzar un segundo enrutado real."""
    con.execute("UPDATE eventos SET estado = 'pendiente', ruta = NULL, motivo = NULL WHERE id = ?", (i,))


def test_reenrutar_un_pendiente_no_duplica_seguimientos_ni_citas(cliente, con, ruta_db):
    cal = recibir(cliente, lead(mensaje="Quiero agendar una cita"))
    tib = recibir(cliente, lead(mensaje="info", nombre="Ana Pérez", phone="0987654321", email="ana@example.com"))
    ctx = Contexto(config.cargar_config())
    procesar(ruta_db, ctx=ctx)
    for _ in range(2):
        for i in (cal, tib):
            _reabrir(con, i)
            t = con.execute("SELECT * FROM trabajos WHERE evento_id = ?", (i,)).fetchone()
            etapa_enrutar.enrutar_lead(con, t, ctx)
    assert con.execute("SELECT COUNT(*) FROM seguimientos WHERE evento_id = ?", (tib,)).fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM citas_tentativas WHERE evento_id = ?", (cal,)).fetchone()[0] == 2
    assert len(avisos(con, cal)) == 1
    assert evento(con, cal)["estado"] == "procesado" and evento(con, tib)["estado"] == "procesado"


def test_reclamo_concurrente_nunca_entrega_un_aviso_dos_veces(cliente, con, ruta_db):
    cfg = config.cargar_config()
    ids = [recibir(cliente, lead(mensaje="x", nombre=f"Persona {k}", phone=f"09911122{k:02d}",
                                 email=f"p{k}@example.com")) for k in range(30)]
    t0 = ahora()
    con.execute("BEGIN IMMEDIATE")
    for i in ids:
        despachador.crear_aviso(con, i, "lead_caliente", evento(con, i)["received_at"], cfg, t0)
    con.execute("COMMIT")
    reclamados, barrera = [], threading.Barrier(8)

    def reclamar():
        c = db.conectar(ruta_db)
        try:
            barrera.wait()
            while (a := reclamar_aviso(c, t0 + timedelta(seconds=1))) is not None:
                reclamados.append(a["id"])
        finally:
            c.close()

    hilos = [threading.Thread(target=reclamar) for _ in range(8)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert sorted(reclamados) == sorted(a["id"] for a in avisos(con))  # cada uno, exactamente una vez


def test_enrutar_no_toca_rechazados_ni_duplicados(cliente, con, ruta_db):
    rech = recibir(cliente, lead(phone="1111111111", email=None))
    recibir(cliente, lead(mensaje="Quiero agendar una cita"))
    dup = recibir(cliente, lead(mensaje="Hola otra vez"))
    ctx = Contexto(config.cargar_config())
    procesar(ruta_db, ctx=ctx)
    antes = {i: tuple(evento(con, i)) for i in (rech, dup)}
    for i in (rech, dup):
        t = con.execute("SELECT * FROM trabajos WHERE evento_id = ?", (i,)).fetchone()
        etapa_enrutar.enrutar_lead(con, t, ctx)  # no lanza ni cambia nada
    assert {i: tuple(evento(con, i)) for i in (rech, dup)} == antes
    assert evento(con, rech)["estado"] == "rechazado" and evento(con, dup)["estado"] == "duplicado"
    assert not avisos(con, rech) and not avisos(con, dup)
