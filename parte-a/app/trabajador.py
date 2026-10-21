"""Procesador de trabajos: cola en SQLite con lease, etapas reanudables y reintentos.

Un único hilo consume la cola en orden de llegada (por id), porque la detección de
duplicados necesita ver los eventos en ese orden. Todas las funciones reciben "ahora"
para poder probar el tiempo sin esperas reales.
"""
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config, db, procesador

log = logging.getLogger("leads.trabajador")

# Espera antes del reintento n (1-based); el último valor se repite.
ESPERAS_REINTENTO_S = (30, 120, 600, 1800)


@dataclass(frozen=True)
class Etapa:
    """Una etapa del procesamiento. Debe ser idempotente y reanudable, y gestionar sus
    propias transacciones: el trabajador solo registra que terminó."""
    nombre: str
    ejecutar: Callable  # (con, trabajo, cfg) -> None


def _normalizar_y_duplicados(con, trabajo, cfg: config.Config) -> None:
    procesador.procesar_evento(con, trabajo["evento_id"], ventana_dias=cfg.duplicado_ventana_dias)


# Orden de ejecución. Las siguientes (API de pacientes, clasificación, revisión de mensajes
# de duplicados, avisos) se añaden aquí sin tocar el resto del módulo.
ETAPAS: list[Etapa] = [
    Etapa("normalizar_y_duplicados", _normalizar_y_duplicados),
]


def _txt(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime(db.FORMATO_UTC)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def reclamar_trabajo(con, ahora: datetime, lease_s: int = config.TRABAJO_LEASE_S_POR_DEFECTO):
    """Toma el trabajo pendiente más antiguo (por id) ya vencido. Devuelve la fila o None."""
    con.execute("BEGIN IMMEDIATE")
    try:
        fila = con.execute(
            """SELECT id FROM trabajos
               WHERE estado = 'pendiente'
                 AND (proximo_intento_at IS NULL OR proximo_intento_at <= ?)
               ORDER BY id LIMIT 1""",
            (_txt(ahora),),
        ).fetchone()
        if fila is not None:
            con.execute(
                """UPDATE trabajos SET estado = 'en_curso', lease_hasta = ?, intentos = intentos + 1
                   WHERE id = ?""",
                (_txt(ahora + timedelta(seconds=lease_s)), fila["id"]),
            )
            fila = con.execute("SELECT * FROM trabajos WHERE id = ?", (fila["id"],)).fetchone()
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return fila


def recuperar_expirados(con, ahora: datetime) -> int:
    """Trabajos 'en_curso' con lease vencida vuelven a 'pendiente'. Devuelve cuántos.

    Si ya agotaron sus intentos (el proceso murió en el último), pasan a 'fallido'.
    """
    t = _txt(ahora)
    con.execute("BEGIN IMMEDIATE")
    try:
        agotados = con.execute(
            """SELECT * FROM trabajos
               WHERE estado = 'en_curso' AND lease_hasta < ? AND intentos >= max_intentos""",
            (t,),
        ).fetchall()
        for trabajo in agotados:
            _marcar_fallido(con, trabajo, "LeaseVencido")
        cur = con.execute(
            """UPDATE trabajos SET estado = 'pendiente', lease_hasta = NULL, proximo_intento_at = ?
               WHERE estado = 'en_curso' AND lease_hasta < ?""",
            (t, t),
        )
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    if cur.rowcount or agotados:
        log.warning("trabajos con lease vencida: %d recuperados, %d fallidos",
                    cur.rowcount, len(agotados))
    return cur.rowcount


def _marcar_fallido(con, trabajo, tipo_error: str) -> None:
    """Dentro de una transacción abierta. Nunca pisa un evento ya rechazado o duplicado."""
    con.execute(
        """UPDATE trabajos SET estado = 'fallido', lease_hasta = NULL, ultimo_error = ?
           WHERE id = ?""",
        (tipo_error, trabajo["id"]),
    )
    con.execute(
        "UPDATE eventos SET estado = 'fallido', motivo = ? WHERE id = ? AND estado = 'pendiente'",
        (f"Procesamiento fallido tras {trabajo['intentos']} intentos ({tipo_error})",
         trabajo["evento_id"]),
    )


# Las escrituras del trabajador exigen seguir siendo dueño del trabajo: mismo estado e
# intentos que al reclamarlo (si la lease venció y otro lo reclamó, intentos cambió).
_ES_DUENO = "id = ? AND estado = 'en_curso' AND intentos = ?"


def _registrar_fallo(con, trabajo, tipo_error: str, ahora: datetime) -> str:
    con.execute("BEGIN IMMEDIATE")
    try:
        if not con.execute(f"SELECT 1 FROM trabajos WHERE {_ES_DUENO}",
                           (trabajo["id"], trabajo["intentos"])).fetchone():
            con.execute("COMMIT")
            return "perdido"
        if trabajo["intentos"] < trabajo["max_intentos"]:
            espera = ESPERAS_REINTENTO_S[min(trabajo["intentos"], len(ESPERAS_REINTENTO_S)) - 1]
            con.execute(
                """UPDATE trabajos SET estado = 'pendiente', lease_hasta = NULL,
                                       proximo_intento_at = ?, ultimo_error = ?
                   WHERE id = ?""",
                (_txt(ahora + timedelta(seconds=espera)), tipo_error, trabajo["id"]),
            )
            resultado = "pendiente"
        else:
            _marcar_fallido(con, trabajo, tipo_error)
            resultado = "fallido"
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return resultado


def ejecutar_trabajo(con, trabajo, ahora: datetime, cfg: config.Config | None = None) -> str:
    """Corre las etapas que faltan. Devuelve 'hecho', 'pendiente', 'fallido' o 'perdido'."""
    cfg = cfg or config.cargar_config()
    nombres = [e.nombre for e in ETAPAS]
    # Etapa desconocida (p. ej. renombrada): se repiten todas, son idempotentes.
    inicio = nombres.index(trabajo["etapa"]) + 1 if trabajo["etapa"] in nombres else 0
    for etapa in ETAPAS[inicio:]:
        try:
            etapa.ejecutar(con, trabajo, cfg)
        except Exception as e:
            if con.in_transaction:
                con.execute("ROLLBACK")
            tipo = type(e).__name__  # solo el tipo: el mensaje puede llevar datos del lead
            log.error("trabajo=%s etapa=%s intento=%s error=%s",
                      trabajo["id"], etapa.nombre, trabajo["intentos"], tipo)
            return _registrar_fallo(con, trabajo, tipo, ahora)
        con.execute(f"UPDATE trabajos SET etapa = ? WHERE {_ES_DUENO}",
                    (etapa.nombre, trabajo["id"], trabajo["intentos"]))
    cur = con.execute(f"UPDATE trabajos SET estado = 'hecho', lease_hasta = NULL WHERE {_ES_DUENO}",
                      (trabajo["id"], trabajo["intentos"]))
    return "hecho" if cur.rowcount == 1 else "perdido"


def procesar_pendientes(db_path: Path, ahora: datetime | None = None,
                        cfg: config.Config | None = None,
                        parar: threading.Event | None = None) -> int:
    """Procesa trabajos hasta que no quede ninguno reclamable. Devuelve cuántos ejecutó.

    Con "ahora" fijo, un trabajo reprogramado para más tarde no se vuelve a tomar.
    """
    cfg = cfg or config.cargar_config()
    con = db.conectar(db_path)
    try:
        ejecutados = 0
        while parar is None or not parar.is_set():
            momento = ahora or _ahora()
            recuperar_expirados(con, momento)
            trabajo = reclamar_trabajo(con, momento, cfg.trabajo_lease_s)
            if trabajo is None:
                break
            ejecutar_trabajo(con, trabajo, momento, cfg)
            ejecutados += 1
        return ejecutados
    finally:
        con.close()


class Trabajador:
    """Hilo demonio único que vacía la cola y espera WORKER_INTERVALO_S cuando no hay trabajo."""

    def __init__(self, db_path: Path, cfg: config.Config):
        self.db_path = db_path
        self.cfg = cfg
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None

    def iniciar(self) -> None:
        self._hilo = threading.Thread(target=self._bucle, name="trabajador-leads", daemon=True)
        self._hilo.start()
        log.info("trabajador iniciado")

    def vivo(self) -> bool:
        return self._hilo is not None and self._hilo.is_alive()

    def detener(self, timeout: float = 30) -> None:
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(timeout)
        log.info("trabajador detenido")

    def _bucle(self) -> None:
        while not self._parar.is_set():
            try:
                ejecutados = procesar_pendientes(self.db_path, cfg=self.cfg, parar=self._parar)
            except Exception as e:  # el hilo nunca muere por un error inesperado
                log.error("error en el bucle del trabajador: %s", type(e).__name__)
                ejecutados = 0
            if ejecutados == 0:
                self._parar.wait(self.cfg.worker_intervalo_s)
