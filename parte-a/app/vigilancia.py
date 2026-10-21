"""Vigilancia interna (D20): la ejecuta el despachador cada VIGILANCIA_CADA_S.

- Eventos 'pendiente' más antiguos que PENDIENTE_ANTIGUO_S -> pendiente_antiguo, con el conteo y
  la antigüedad máxima (sin ids ni datos personales).
- Un hilo de fondo que no está vivo -> hilo_caido, con su nombre como origen. El despachador
  vigila al trabajador y el trabajador al despachador (un hilo no puede detectar su propia caída).
- Los trabajos y avisos fallidos ya se reportan al ocurrir (evento_fallido, aviso_fallido):
  no se duplican aquí.

La deduplicación la hace la ventana de incidencias de reportar_error.
"""
import logging
from datetime import datetime, timedelta, timezone

from app import config, db
from app.errores import reportar_error

HILOS = ("trabajador", "despachador")


def revisar_hilos(hilos: dict | None) -> list[str]:
    caidos = [n for n, vivo in (hilos or {}).items() if n in HILOS and vivo is False]
    for nombre in caidos:
        reportar_error(nombre, "hilo_caido", nivel=logging.ERROR)
    return caidos


def vigilar(con, ahora: datetime, cfg: config.Config, hilos: dict | None = None) -> list[str]:
    """Devuelve los códigos reportados en esta pasada."""
    reportados = []
    limite = (ahora - timedelta(seconds=cfg.pendiente_antiguo_s)).astimezone(timezone.utc).strftime(db.FORMATO_UTC)
    fila = con.execute("SELECT COUNT(*), MIN(received_at) FROM eventos WHERE estado = 'pendiente' AND received_at < ?",
                       (limite,)).fetchone()
    if fila[0]:
        mas_antiguo = datetime.strptime(fila[1], db.FORMATO_UTC).replace(tzinfo=timezone.utc)
        reportar_error("vigilancia", "pendiente_antiguo", nivel=logging.ERROR,
                       datos={"conteo": fila[0], "antiguedad_max_s": int((ahora - mas_antiguo).total_seconds())})
        reportados.append("pendiente_antiguo")
    if revisar_hilos(hilos):
        reportados.append("hilo_caido")
    return reportados
