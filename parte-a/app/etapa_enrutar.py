"""Etapa enrutar_lead (D15): última etapa; cierra el evento según su ruta.

- caliente: citas tentativas + aviso a recepción (bandeja de avisos).
- tibio: primer mensaje de seguimiento.
- basura: nada (el motivo ya está en la clasificación).

El evento pasa a 'procesado' en la MISMA transacción que crea el aviso o el seguimiento
(patrón outbox): un caliente nunca queda 'procesado' sin su aviso registrado. Si algo falla
antes del COMMIT, el evento sigue 'pendiente' y la etapa se reintenta. Nunca pisa un
rechazado ni un duplicado. Idempotente: repetirla no duplica filas.
"""
import logging
from datetime import datetime, timezone

from app import agenda, despachador, plantillas

log = logging.getLogger("leads.enrutado")

NOMBRE = "enrutar_lead"


def _ahora(ctx) -> datetime:
    reloj = getattr(ctx, "reloj", None)
    return reloj() if reloj is not None else datetime.now(timezone.utc)


def enrutar_lead(con, trabajo, ctx) -> None:
    evento_id = trabajo["evento_id"]
    ev = con.execute("SELECT estado, lead_id FROM eventos WHERE id = ?", (evento_id,)).fetchone()
    if ev is None or ev["estado"] != "pendiente" or ev["lead_id"] is None:
        return  # rechazado, duplicado, fallido o ya procesado
    lead_id = ev["lead_id"]
    cla = con.execute("SELECT ruta, motivo FROM clasificaciones WHERE evento_id = ?", (evento_id,)).fetchone()
    if cla is None:
        raise LookupError("evento sin clasificación")  # error de etapa: se reintenta
    lead = con.execute("SELECT nombre, servicio FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
    enr = con.execute("SELECT coincidencia, sede_habitual FROM enriquecimientos WHERE evento_id = ?",
                      (evento_id,)).fetchone()
    sede_habitual = enr["sede_habitual"] if enr is not None and enr["coincidencia"] == "unica" else None
    cfg, ahora = ctx.cfg, _ahora(ctx)

    con.execute("BEGIN IMMEDIATE")
    try:
        # Volver a comprobar dentro del bloqueo de escritura.
        ev = con.execute("SELECT estado, received_at FROM eventos WHERE id = ?", (evento_id,)).fetchone()
        if ev["estado"] != "pendiente":
            con.execute("COMMIT")
            return
        if cla["ruta"] in ("caliente", "tibio"):
            citas = agenda.proponer_citas(lead["servicio"], sede_habitual, ahora, cfg.feriados)
        if cla["ruta"] == "caliente":
            for c in citas:
                con.execute(
                    """INSERT INTO citas_tentativas (evento_id, orden, servicio_propuesto, sede, inicio, fin,
                                                     precio_usd, nota)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(evento_id, orden) DO NOTHING""",
                    (evento_id, c.orden, c.servicio_propuesto, c.sede, c.inicio.isoformat(),
                     c.fin.isoformat(), c.precio_usd, c.nota))
            despachador.crear_aviso(con, evento_id, "lead_caliente", ev["received_at"], cfg, ahora)
        elif cla["ruta"] == "tibio":
            mensaje = plantillas.seguimiento_tibio(lead["nombre"], lead["servicio"], citas, cfg.agenda_url)
            con.execute(
                """INSERT INTO seguimientos (evento_id, lead_id, mensaje, estado, creado_at)
                   VALUES (?, ?, ?, 'pendiente', ?) ON CONFLICT(evento_id) DO NOTHING""",
                (evento_id, lead_id, mensaje, despachador._txt(ahora)))
        # Cierre del evento, en la misma transacción. Solo si sigue pendiente.
        con.execute(
            "UPDATE eventos SET estado = 'procesado', ruta = ?, motivo = ? WHERE id = ? AND estado = 'pendiente'",
            (cla["ruta"], cla["motivo"] if cla["ruta"] == "basura" else None, evento_id))
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    log.info("evento=%s enrutado ruta=%s", evento_id, cla["ruta"])
