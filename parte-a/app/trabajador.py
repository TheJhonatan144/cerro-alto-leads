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

from app import config, db, enriquecimiento, procesador
from app.errores import MAX_REPROGRAMACIONES, Reprogramar
from app.pacientes import ClientePacientes

log = logging.getLogger("leads.trabajador")

# Espera antes del reintento n (1-based); el último valor se repite.
ESPERAS_REINTENTO_S = (30, 120, 600, 1800)
# Pausa fija tras un error inesperado del bucle (no depende de la configuración).
ESPERA_TRAS_ERROR_S = 1.0


@dataclass(frozen=True)
class Etapa:
    """Una etapa del procesamiento. Debe ser idempotente y reanudable, y gestionar sus
    propias transacciones: el trabajador solo registra que terminó. Puede lanzar
    Reprogramar para volver más tarde sin consumir intento."""
    nombre: str
    ejecutar: Callable  # (con, trabajo, ctx: Contexto) -> None


@dataclass
class Contexto:
    """Recursos compartidos por las etapas durante la vida del trabajador."""
    cfg: config.Config
    pacientes: ClientePacientes | None = None  # None: API no configurada

    def cerrar(self) -> None:
        if self.pacientes is not None:
            self.pacientes.cerrar()


def crear_contexto(cfg: config.Config) -> Contexto:
    if not cfg.pacientes_configurada:
        return Contexto(cfg)
    return Contexto(cfg, ClientePacientes(cfg.api_pacientes_url, cfg.api_pacientes_token,
                                          limite_por_min=cfg.pacientes_limite_por_min,
                                          max_paginas=cfg.pacientes_max_paginas))


def _normalizar_y_duplicados(con, trabajo, ctx: Contexto) -> None:
    procesador.procesar_evento(con, trabajo["evento_id"],
                               ventana_dias=ctx.cfg.duplicado_ventana_dias)


# Orden de ejecución. Las siguientes (clasificación, revisión de mensajes de duplicados,
# avisos) se añaden aquí sin tocar el resto del módulo.
ETAPAS: list[Etapa] = [
    Etapa("normalizar_y_duplicados", _normalizar_y_duplicados),
    Etapa(enriquecimiento.NOMBRE, enriquecimiento.enriquecer_paciente),
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


def _reloj(ahora) -> Callable[[], datetime]:
    """'ahora' puede ser un instante fijo, un reloj (callable) o None (reloj real)."""
    if callable(ahora):
        return ahora
    if ahora is not None:
        return lambda: ahora
    return lambda: _ahora()  # se resuelve en cada llamada: los tests pueden sustituir _ahora


def _renovar_si_dueno(con, trabajo, momento: datetime, lease_s: int) -> bool:
    """Comprueba con una escritura que el trabajo sigue siendo nuestro (mismos intentos y
    lease vigente) y renueva la lease. False si afecta cero filas."""
    cur = con.execute(
        f"UPDATE trabajos SET lease_hasta = ? WHERE {_ES_DUENO} AND lease_hasta >= ?",
        (_txt(momento + timedelta(seconds=lease_s)), trabajo["id"], trabajo["intentos"],
         _txt(momento)),
    )
    return cur.rowcount == 1


def _reprogramar(con, trabajo, segundos: float, momento: datetime) -> str:
    """Vuelve a 'pendiente' para dentro de 'segundos', libera la lease y devuelve el intento."""
    cur = con.execute(
        f"""UPDATE trabajos SET estado = 'pendiente', lease_hasta = NULL, proximo_intento_at = ?,
                               intentos = intentos - 1, reprogramaciones = reprogramaciones + 1
            WHERE {_ES_DUENO}""",
        (_txt(momento + timedelta(seconds=segundos)), trabajo["id"], trabajo["intentos"]),
    )
    if cur.rowcount != 1:
        return "perdido"
    log.info("trabajo=%s reprogramado en %.0f s (%d/%d)", trabajo["id"], segundos,
             trabajo["reprogramaciones"] + 1, MAX_REPROGRAMACIONES)
    return "reprogramado"


def ejecutar_trabajo(con, trabajo, ahora, cfg: config.Config | None = None,
                     ctx: Contexto | None = None) -> str:
    """Corre las etapas que faltan. Devuelve 'hecho', 'pendiente', 'reprogramado',
    'fallido' o 'perdido'.

    'ahora' es un instante fijo o un reloj; se consulta antes de cada etapa y al fallar.
    """
    propio = ctx is None
    ctx = ctx or crear_contexto(cfg or config.cargar_config())
    try:
        return _ejecutar(con, trabajo, _reloj(ahora), ctx)
    finally:
        if propio:
            ctx.cerrar()


def _ejecutar(con, trabajo, reloj, ctx: Contexto) -> str:
    cfg = ctx.cfg
    nombres = [e.nombre for e in ETAPAS]
    # Etapa desconocida (p. ej. renombrada): se repiten todas, son idempotentes.
    inicio = nombres.index(trabajo["etapa"]) + 1 if trabajo["etapa"] in nombres else 0
    for etapa in ETAPAS[inicio:]:
        if not _renovar_si_dueno(con, trabajo, reloj(), cfg.trabajo_lease_s):
            log.warning("trabajo=%s ya no es nuestro (lease vencida o reclamado); "
                        "no se ejecuta la etapa %s", trabajo["id"], etapa.nombre)
            return "perdido"
        try:
            etapa.ejecutar(con, trabajo, ctx)
        except Reprogramar as r:
            if con.in_transaction:
                con.execute("ROLLBACK")
            if trabajo["reprogramaciones"] < MAX_REPROGRAMACIONES:
                return _reprogramar(con, trabajo, r.segundos, reloj())
            # Agotadas: la etapa debía degradar. Por seguridad cuenta como fallo normal.
            log.error("trabajo=%s etapa=%s reprogramaciones agotadas", trabajo["id"], etapa.nombre)
            return _registrar_fallo(con, trabajo, "ReprogramacionesAgotadas", reloj())
        except Exception as e:
            if con.in_transaction:
                con.execute("ROLLBACK")
            tipo = type(e).__name__  # solo el tipo: el mensaje puede llevar datos del lead
            log.error("trabajo=%s etapa=%s intento=%s error=%s",
                      trabajo["id"], etapa.nombre, trabajo["intentos"], tipo)
            # La espera se cuenta desde el fallo, no desde el reclamo.
            return _registrar_fallo(con, trabajo, tipo, reloj())
        cur = con.execute(f"UPDATE trabajos SET etapa = ? WHERE {_ES_DUENO}",
                          (etapa.nombre, trabajo["id"], trabajo["intentos"]))
        if cur.rowcount != 1:
            log.warning("trabajo=%s ya no es nuestro tras la etapa %s; se interrumpe",
                        trabajo["id"], etapa.nombre)
            return "perdido"
    cur = con.execute(f"UPDATE trabajos SET estado = 'hecho', lease_hasta = NULL WHERE {_ES_DUENO}",
                      (trabajo["id"], trabajo["intentos"]))
    return "hecho" if cur.rowcount == 1 else "perdido"


def procesar_pendientes(db_path: Path, ahora=None,
                        cfg: config.Config | None = None,
                        parar: threading.Event | None = None,
                        ctx: Contexto | None = None) -> int:
    """Procesa trabajos hasta que no quede ninguno reclamable. Devuelve cuántos ejecutó.

    'ahora': instante fijo, reloj (callable) o None (reloj real). Con un instante fijo, un
    trabajo reprogramado para más tarde no se vuelve a tomar.
    """
    propio = ctx is None
    ctx = ctx or crear_contexto(cfg or config.cargar_config())
    cfg = ctx.cfg
    reloj = _reloj(ahora)
    con = db.conectar(db_path)
    try:
        ejecutados = 0
        while parar is None or not parar.is_set():
            momento = reloj()
            recuperar_expirados(con, momento)
            trabajo = reclamar_trabajo(con, momento, cfg.trabajo_lease_s)
            if trabajo is None:
                break
            ejecutar_trabajo(con, trabajo, reloj, ctx=ctx)
            ejecutados += 1
        return ejecutados
    finally:
        con.close()
        if propio:
            ctx.cerrar()


class Trabajador:
    """Hilo demonio único que vacía la cola y espera WORKER_INTERVALO_S cuando no hay trabajo."""

    def __init__(self, db_path: Path, cfg: config.Config, ctx: Contexto | None = None):
        self.db_path = db_path
        self.cfg = cfg
        self.ctx = ctx or crear_contexto(cfg)  # un cliente y un limitador por proceso
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None

    def iniciar(self) -> None:
        self._hilo = threading.Thread(target=self._bucle, name="trabajador-leads", daemon=True)
        self._hilo.start()
        log.info("trabajador iniciado")

    def vivo(self) -> bool:
        return self._hilo is not None and self._hilo.is_alive()

    def detener(self, timeout: float = 30) -> bool:
        """Pide parar y espera hasta 'timeout' s. True si el hilo terminó."""
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(timeout)
            if self._hilo.is_alive():
                # Un trabajo a medias queda 'en_curso' y se recupera cuando vence su lease.
                log.warning("trabajador no terminó a tiempo; se recuperará por lease")
                return False
        self.ctx.cerrar()
        log.info("trabajador detenido")
        return True

    def _bucle(self) -> None:
        while not self._parar.is_set():
            try:  # la espera también va dentro: ningún error mata el hilo
                if procesar_pendientes(self.db_path, parar=self._parar, ctx=self.ctx) == 0:
                    self._parar.wait(self.cfg.worker_intervalo_s)
            except Exception as e:
                log.error("error en el bucle del trabajador: %s", type(e).__name__)
                self._parar.wait(ESPERA_TRAS_ERROR_S)
