"""Bandeja de avisos (outbox) y su despachador.

Los avisos se crean en la misma transacción que cierra el evento (etapa enrutar_lead) y
este módulo los envía por el canal configurado. Todo el estado vive en la base: tras un
reinicio, el despachador retoma lo pendiente. Semántica de "al menos una vez": si el proceso
muere entre el envío y el registro de "enviado", el aviso se reenvía.

Política de reintentos (D13): tras cada fallo, 5 s, 15 s, 45 s y 90 s; después cada 120 s
hasta 10 intentos. Si pasa vence_at sin enviarse, se avisa una sola vez por el canal de
errores. Agotados los intentos, el aviso queda 'fallido'.
"""
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import agenda, config, db, errores, plantillas, vigilancia
# Los adaptadores viven en app/canales.py; se reexportan aquí por compatibilidad.
from app.canales import CanalArchivo, ErrorCanal, crear_canal  # noqa: F401
from app.errores import reportar_error
from app.pacientes import Presupuesto

log = logging.getLogger("leads.despachador")

ESPERAS_REINTENTO_S = (5, 15, 45, 90)
ESPERA_MAXIMA_S = 120
MAX_INTENTOS = 10
PRESUPUESTO_ENVIO_S = 10.0
LEASE_S = 60  # mayor que el envío por hasta 3 canales (3 x 10 s) más margen
INTERVALO_S = 1.0
ESPERA_TRAS_ERROR_S = 1.0
TIPOS = ("lead_caliente", "novedad_duplicado")
# Incidencias del canal de errores (D19): 10 s, 1 min, 5 min y 15 min; después, fallida.
ESPERAS_INCIDENCIA_S = (10, 60, 300, 900)
MAX_INTENTOS_INCIDENCIA = 5
_TABLAS_COLA = ("avisos", "incidencias")


def _txt(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime(db.FORMATO_UTC)


def _leer(texto: str) -> datetime:
    return datetime.strptime(texto, db.FORMATO_UTC).replace(tzinfo=timezone.utc)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# --- Creación (la usan las etapas, dentro de su transacción) --------------------------------

def crear_aviso(con, evento_id: int, tipo: str, recibido_at: str, cfg: config.Config,
                ahora: datetime) -> bool:
    """Registra un aviso pendiente. Idempotente: (evento_id, tipo) es único. Debe llamarse
    dentro de una transacción abierta. Devuelve True si lo creó."""
    vence = _leer(recibido_at) + timedelta(seconds=cfg.aviso_plazo_s)
    cur = con.execute(
        """INSERT INTO avisos (evento_id, tipo, canal, estado, intentos, proximo_intento, vence_at, creado_at)
           VALUES (?, ?, ?, 'pendiente', 0, ?, ?, ?)
           ON CONFLICT(evento_id, tipo) DO NOTHING""",
        (evento_id, tipo, cfg.avisos_canal, _txt(ahora), _txt(vence), _txt(ahora)),
    )
    return cur.rowcount == 1


# --- Renderizado (desde la base, en el momento del envío) -----------------------------------------

def _citas(con, evento_id: int) -> list[agenda.Propuesta]:
    citas = []
    for f in con.execute("SELECT * FROM citas_tentativas WHERE evento_id = ? ORDER BY orden", (evento_id,)):
        inicio, fin = datetime.fromisoformat(f["inicio"]), datetime.fromisoformat(f["fin"])
        citas.append(agenda.Propuesta(
            orden=f["orden"], clave="", servicio_propuesto=f["servicio_propuesto"], servicio_interes=None,
            sede=f["sede"], inicio=inicio, fin=fin, duracion_min=int((fin - inicio).total_seconds() // 60),
            precio_usd=f["precio_usd"], nota=f["nota"]))
    return citas


def renderizar(con, aviso) -> str:
    if aviso["tipo"] == "lead_caliente":
        f = con.execute(
            """SELECT e.event_id, e.received_at, e.lead_id, l.nombre, l.telefono_e164, l.email, l.servicio,
                      c.ruta, c.motivo, n.estado_enriquecimiento, n.tratamientos_pendientes
               FROM eventos e JOIN leads l ON l.lead_id = e.lead_id
               JOIN clasificaciones c ON c.evento_id = e.id
               LEFT JOIN enriquecimientos n ON n.evento_id = e.id
               WHERE e.id = ?""", (aviso["evento_id"],)).fetchone()
        if f is None:
            raise LookupError("datos del aviso incompletos")
        return plantillas.aviso_caliente({**dict(f), "citas": _citas(con, aviso["evento_id"])})
    f = con.execute(
        """SELECT e.event_id, e.received_at, nd.duplicado_de, nd.lead_id, nd.senales,
                  l.nombre, l.telefono_e164, l.email
           FROM novedades_duplicado nd JOIN eventos e ON e.id = nd.evento_id
           JOIN leads l ON l.lead_id = nd.lead_id
           WHERE nd.evento_id = ?""", (aviso["evento_id"],)).fetchone()
    if f is None:
        raise LookupError("datos del aviso incompletos")
    return plantillas.novedad_duplicado({**dict(f), "senales": json.loads(f["senales"])})


# --- Cola ------------------------------------------------------------------------------------------

def _reclamar(con, tabla: str, ahora: datetime):
    """Toma la fila pendiente más antigua ya vencida y sin reserva vigente; suma un intento."""
    if tabla not in _TABLAS_COLA:
        raise ValueError("tabla de cola desconocida")
    t = _txt(ahora)
    con.execute("BEGIN IMMEDIATE")
    try:
        fila = con.execute(
            f"""SELECT id FROM {tabla}
                WHERE estado = 'pendiente' AND proximo_intento <= ?
                  AND (lease_hasta IS NULL OR lease_hasta < ?)
                ORDER BY id LIMIT 1""", (t, t)).fetchone()
        if fila is not None:
            con.execute(f"UPDATE {tabla} SET lease_hasta = ?, intentos = intentos + 1 WHERE id = ?",
                        (_txt(ahora + timedelta(seconds=LEASE_S)), fila["id"]))
            fila = con.execute(f"SELECT * FROM {tabla} WHERE id = ?", (fila["id"],)).fetchone()
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return fila


def reclamar_aviso(con, ahora: datetime):
    return _reclamar(con, "avisos", ahora)


def reclamar_incidencia(con, ahora: datetime):
    return _reclamar(con, "incidencias", ahora)


def revisar_vencidos(con, ahora: datetime) -> int:
    """Avisos pendientes que pasaron vence_at: se notifica una sola vez por aviso. La marca
    vencido_notificado y la incidencia aviso_vencido van en la MISMA transacción: si una
    falla, ninguna queda y el aviso se revisa otra vez en la siguiente vuelta."""
    notificados = 0
    for f in con.execute("""SELECT id, evento_id, tipo FROM avisos
                            WHERE estado = 'pendiente' AND vencido_notificado = 0 AND vence_at < ?""",
                         (_txt(ahora),)).fetchall():
        try:
            con.execute("BEGIN IMMEDIATE")
            anotar = None
            cur = con.execute("UPDATE avisos SET vencido_notificado = 1 WHERE id = ? AND vencido_notificado = 0",
                              (f["id"],))
            if cur.rowcount == 1:
                anotar = errores.registrar_en_transaccion(con, "avisos", "aviso_vencido", evento_id=f["evento_id"])
            con.execute("COMMIT")
        except Exception as e:  # solo el tipo; se reintenta en la siguiente vuelta
            if con.in_transaction:
                con.execute("ROLLBACK")
            log.warning("aviso=%s vencimiento no registrado: %s", f["id"], type(e).__name__)
            continue
        if anotar is not None:
            anotar(logging.ERROR)
            notificados += 1
    return notificados


_ES_DUENO = "id = ? AND estado = 'pendiente' AND intentos = ?"


def _espera(intentos: int) -> int:
    return ESPERAS_REINTENTO_S[intentos - 1] if intentos <= len(ESPERAS_REINTENTO_S) else ESPERA_MAXIMA_S


def _registrar_fallo(con, aviso, codigo: str, permanente: bool, momento: datetime) -> str:
    if permanente or aviso["intentos"] >= MAX_INTENTOS:
        cur = con.execute(f"""UPDATE avisos SET estado = 'fallido', lease_hasta = NULL, ultimo_error = ?
                              WHERE {_ES_DUENO}""", (codigo, aviso["id"], aviso["intentos"]))
        if cur.rowcount == 1:
            reportar_error("avisos", "aviso_fallido", evento_id=aviso["evento_id"], nivel=logging.ERROR)
        return "fallido"
    # La espera se cuenta desde el fallo.
    proximo = momento + timedelta(seconds=_espera(aviso["intentos"]))
    con.execute(f"""UPDATE avisos SET lease_hasta = NULL, proximo_intento = ?, ultimo_error = ?
                    WHERE {_ES_DUENO}""", (_txt(proximo), codigo, aviso["id"], aviso["intentos"]))
    log.warning("aviso=%s intento=%s fallo=%s", aviso["id"], aviso["intentos"], codigo)
    return "pendiente"


def _entregar(con, tabla: str, registro, texto: str, canal) -> None:
    """D18 corregida: con notificadores, el archivo es auditoría (una línea por registro, no
    cuenta como entrega) y solo un notificador entrega. Sin notificadores (o con un canal
    simple), el envío del canal es la entrega. Lanza ErrorCanal si no se entregó."""
    if tabla not in _TABLAS_COLA:
        raise ValueError("tabla de cola desconocida")
    presupuesto = Presupuesto(PRESUPUESTO_ENVIO_S)
    if getattr(canal, "notificadores", None):
        if not registro["archivado"] and canal.archivar(texto, registro, presupuesto):
            con.execute(f"UPDATE {tabla} SET archivado = 1 WHERE id = ?", (registro["id"],))
        canal.notificar(texto, registro, presupuesto)
        return
    canal.enviar(texto, registro, presupuesto)
    if getattr(canal, "auditoria", None):  # solo archivo: la entrega es la línea escrita
        con.execute(f"UPDATE {tabla} SET archivado = 1 WHERE id = ?", (registro["id"],))


def enviar_aviso(con, aviso, canal, reloj: Callable[[], datetime] = _ahora) -> str:
    """Renderiza y envía un aviso reclamado. Devuelve 'enviado', 'pendiente' o 'fallido'."""
    try:
        texto = renderizar(con, aviso)
        _entregar(con, "avisos", aviso, texto, canal)
    except ErrorCanal as e:
        return _registrar_fallo(con, aviso, e.codigo, e.tipo == "permanente", reloj())
    except Exception as e:  # solo el tipo: el mensaje podría llevar datos del lead
        return _registrar_fallo(con, aviso, type(e).__name__, False, reloj())
    momento = reloj()
    recibido = con.execute("SELECT received_at FROM eventos WHERE id = ?", (aviso["evento_id"],)).fetchone()
    latencia = (momento - _leer(recibido["received_at"])).total_seconds() if recibido else None
    con.execute(f"""UPDATE avisos SET estado = 'enviado', enviado_at = ?, latencia_s = ?, lease_hasta = NULL
                    WHERE {_ES_DUENO}""", (_txt(momento), latencia, aviso["id"], aviso["intentos"]))
    log.info("aviso=%s tipo=%s enviado intento=%s latencia_s=%.1f", aviso["id"], aviso["tipo"],
             aviso["intentos"], latencia or 0)
    return "enviado"


def _fallo_incidencia(con, inc, codigo: str, permanente: bool, momento: datetime) -> str:
    """Nunca llama a reportar_error: un fallo del canal de errores no genera otra incidencia."""
    dueno = (inc["id"], inc["intentos"])
    if permanente or inc["intentos"] >= MAX_INTENTOS_INCIDENCIA:
        con.execute(f"UPDATE incidencias SET estado = 'fallido', lease_hasta = NULL, ultimo_error = ? "
                    f"WHERE {_ES_DUENO}", (codigo, *dueno))
        log.error("incidencia=%s codigo=%s no se pudo enviar: fallida", inc["id"], inc["codigo"])
        return "fallido"
    espera = ESPERAS_INCIDENCIA_S[min(inc["intentos"], len(ESPERAS_INCIDENCIA_S)) - 1]
    con.execute(f"UPDATE incidencias SET lease_hasta = NULL, proximo_intento = ?, ultimo_error = ? "
                f"WHERE {_ES_DUENO}", (_txt(momento + timedelta(seconds=espera)), codigo, *dueno))
    log.warning("incidencia=%s intento=%s fallo=%s", inc["id"], inc["intentos"], codigo)
    return "pendiente"


def enviar_incidencia(con, inc, canal, reloj: Callable[[], datetime] = _ahora) -> str:
    try:
        _entregar(con, "incidencias", inc, plantillas.incidencia(dict(inc)), canal)
    except ErrorCanal as e:
        return _fallo_incidencia(con, inc, e.codigo, e.tipo == "permanente", reloj())
    except Exception as e:
        return _fallo_incidencia(con, inc, type(e).__name__, False, reloj())
    con.execute(f"UPDATE incidencias SET estado = 'enviado', enviado_at = ?, lease_hasta = NULL WHERE {_ES_DUENO}",
                (_txt(reloj()), inc["id"], inc["intentos"]))
    return "enviado"


def _reloj(ahora) -> Callable[[], datetime]:
    return ahora if callable(ahora) else ((lambda: ahora) if ahora is not None else (lambda: _ahora()))


def despachar_incidencias(db_path: Path, canal, ahora=None, parar: threading.Event | None = None) -> int:
    reloj = _reloj(ahora)
    con = db.conectar(db_path)
    try:
        enviadas = 0
        while parar is None or not parar.is_set():
            inc = reclamar_incidencia(con, reloj())
            if inc is None:
                break
            enviar_incidencia(con, inc, canal, reloj)
            enviadas += 1
        return enviadas
    finally:
        con.close()


def despachar_pendientes(db_path: Path, canal, ahora=None, parar: threading.Event | None = None,
                         canal_errores=None) -> int:
    """Envía todo lo reclamable ahora (avisos y, si hay canal de errores, incidencias).
    'ahora': instante fijo, reloj (callable) o None."""
    reloj = _reloj(ahora)
    con = db.conectar(db_path)
    try:
        enviados = 0
        while parar is None or not parar.is_set():
            momento = reloj()
            revisar_vencidos(con, momento)
            aviso = reclamar_aviso(con, momento)
            if aviso is None:
                break
            enviar_aviso(con, aviso, canal, reloj)
            enviados += 1
    finally:
        con.close()
    if canal_errores is not None:
        despachar_incidencias(db_path, canal_errores, reloj, parar)
    return enviados


class Despachador:
    """Hilo demonio que vacía la bandeja de avisos cada INTERVALO_S (como el Trabajador)."""

    def __init__(self, db_path: Path, canal, reloj: Callable[[], datetime] | None = None,
                 intervalo_s: float = INTERVALO_S, canal_errores=None, cfg: config.Config | None = None,
                 hilos: Callable[[], dict] | None = None):
        self.db_path, self.canal, self.reloj, self.intervalo_s = db_path, canal, reloj, intervalo_s
        self.canal_errores = canal_errores
        self.cfg, self.hilos = cfg, hilos  # vigilancia interna (D20); sin cfg no se vigila
        self._ultima_vigilancia: datetime | None = None
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None

    def iniciar(self) -> None:
        self._hilo = threading.Thread(target=self._bucle, name="despachador-avisos", daemon=True)
        self._hilo.start()
        log.info("despachador iniciado (canal %s)", getattr(self.canal, "nombre", "?"))

    def vivo(self) -> bool:
        return self._hilo is not None and self._hilo.is_alive()

    def estado(self) -> bool | None:
        """None si aún no se inició (desconocido, no caído); si no, si sigue vivo."""
        return None if self._hilo is None else self._hilo.is_alive()

    def detener(self, timeout: float = 30) -> bool:
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(timeout)
            if self._hilo.is_alive():
                log.warning("despachador no terminó a tiempo; el aviso en curso se retomará por lease")
                return False
        log.info("despachador detenido")
        return True

    def _tal_vez_vigilar(self, momento: datetime) -> bool:
        """Ejecuta la vigilancia si pasaron VIGILANCIA_CADA_S desde la última."""
        if self.cfg is None:
            return False
        if self._ultima_vigilancia is not None and \
                (momento - self._ultima_vigilancia).total_seconds() < self.cfg.vigilancia_cada_s:
            return False
        self._ultima_vigilancia = momento
        con = db.conectar(self.db_path)
        try:
            vigilancia.vigilar(con, momento, self.cfg, self.hilos() if self.hilos else None)
        finally:
            con.close()
        return True

    def _bucle(self) -> None:
        while not self._parar.is_set():
            try:  # ningún error mata el hilo
                despachar_pendientes(self.db_path, self.canal, self.reloj, self._parar, self.canal_errores)
                self._tal_vez_vigilar(self.reloj() if self.reloj else _ahora())
                self._parar.wait(self.intervalo_s)
            except Exception as e:
                log.error("error en el bucle del despachador: %s", type(e).__name__)
                self._parar.wait(ESPERA_TRAS_ERROR_S)
