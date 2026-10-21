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
import os
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import agenda, config, db, plantillas
from app.errores import reportar_error
from app.pacientes import Presupuesto

log = logging.getLogger("leads.despachador")

ESPERAS_REINTENTO_S = (5, 15, 45, 90)
ESPERA_MAXIMA_S = 120
MAX_INTENTOS = 10
PRESUPUESTO_ENVIO_S = 10.0
LEASE_S = 30  # mayor que el presupuesto de envío
INTERVALO_S = 1.0
ESPERA_TRAS_ERROR_S = 1.0
TIPOS = ("lead_caliente", "novedad_duplicado")


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


# --- Adaptadores de canal ---------------------------------------------------------------------

class ErrorCanal(Exception):
    """Fallo al enviar. tipo: 'temporal' (se reintenta) o 'permanente' (fallido directo).
    codigo: texto corto sin datos personales."""

    def __init__(self, tipo: str, codigo: str):
        if tipo not in ("temporal", "permanente"):
            raise ValueError("tipo de ErrorCanal inválido")
        super().__init__(codigo)
        self.tipo, self.codigo = tipo, codigo


class CanalArchivo:
    """Una línea JSON por aviso. El archivo contiene datos personales: permisos 0600 y debe
    estar fuera del control de versiones (data/ está en .gitignore)."""
    nombre = "archivo"

    def __init__(self, ruta: Path):
        self.ruta = Path(ruta)

    def enviar(self, texto: str, aviso, presupuesto: Presupuesto | None = None) -> None:
        linea = json.dumps({"aviso_id": aviso["id"], "evento_id": aviso["evento_id"],
                            "tipo": aviso["tipo"], "enviado_at": db.ahora_utc(), "texto": texto},
                           ensure_ascii=False) + "\n"
        try:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.ruta, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.fchmod(fd, 0o600)  # también si el archivo ya existía con otros permisos
                os.write(fd, linea.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            raise ErrorCanal("temporal", "archivo_no_escribible") from None


# Registro de adaptadores: nombre de AVISOS_CANAL -> fábrica. El Bloque 5b añade telegram y n8n.
ADAPTADORES: dict[str, Callable[[config.Config], object]] = {
    "archivo": lambda cfg: CanalArchivo(cfg.avisos_archivo),
}


def crear_canal(cfg: config.Config):
    return ADAPTADORES[cfg.avisos_canal](cfg)


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

def reclamar_aviso(con, ahora: datetime):
    """Toma el aviso pendiente más antiguo ya vencido y sin reserva vigente; suma un intento."""
    t = _txt(ahora)
    con.execute("BEGIN IMMEDIATE")
    try:
        fila = con.execute(
            """SELECT id FROM avisos
               WHERE estado = 'pendiente' AND proximo_intento <= ?
                 AND (lease_hasta IS NULL OR lease_hasta < ?)
               ORDER BY id LIMIT 1""", (t, t)).fetchone()
        if fila is not None:
            con.execute("UPDATE avisos SET lease_hasta = ?, intentos = intentos + 1 WHERE id = ?",
                        (_txt(ahora + timedelta(seconds=LEASE_S)), fila["id"]))
            fila = con.execute("SELECT * FROM avisos WHERE id = ?", (fila["id"],)).fetchone()
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return fila


def revisar_vencidos(con, ahora: datetime) -> int:
    """Avisos pendientes que pasaron vence_at: se notifica una sola vez por aviso."""
    notificados = 0
    for f in con.execute("""SELECT id, evento_id, tipo FROM avisos
                            WHERE estado = 'pendiente' AND vencido_notificado = 0 AND vence_at < ?""",
                         (_txt(ahora),)).fetchall():
        cur = con.execute("UPDATE avisos SET vencido_notificado = 1 WHERE id = ? AND vencido_notificado = 0",
                          (f["id"],))
        if cur.rowcount == 1:
            reportar_error("avisos", "aviso_vencido", evento_id=f["evento_id"], nivel=logging.ERROR)
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


def enviar_aviso(con, aviso, canal, reloj: Callable[[], datetime] = _ahora) -> str:
    """Renderiza y envía un aviso reclamado. Devuelve 'enviado', 'pendiente' o 'fallido'."""
    try:
        texto = renderizar(con, aviso)
        canal.enviar(texto, aviso, Presupuesto(PRESUPUESTO_ENVIO_S))
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


def despachar_pendientes(db_path: Path, canal, ahora=None, parar: threading.Event | None = None) -> int:
    """Envía todo lo reclamable ahora. 'ahora': instante fijo, reloj (callable) o None."""
    reloj = ahora if callable(ahora) else ((lambda: ahora) if ahora is not None else (lambda: _ahora()))
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
        return enviados
    finally:
        con.close()


class Despachador:
    """Hilo demonio que vacía la bandeja de avisos cada INTERVALO_S (como el Trabajador)."""

    def __init__(self, db_path: Path, canal, reloj: Callable[[], datetime] | None = None,
                 intervalo_s: float = INTERVALO_S):
        self.db_path, self.canal, self.reloj, self.intervalo_s = db_path, canal, reloj, intervalo_s
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None

    def iniciar(self) -> None:
        self._hilo = threading.Thread(target=self._bucle, name="despachador-avisos", daemon=True)
        self._hilo.start()
        log.info("despachador iniciado (canal %s)", getattr(self.canal, "nombre", "?"))

    def vivo(self) -> bool:
        return self._hilo is not None and self._hilo.is_alive()

    def detener(self, timeout: float = 30) -> bool:
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(timeout)
            if self._hilo.is_alive():
                log.warning("despachador no terminó a tiempo; el aviso en curso se retomará por lease")
                return False
        log.info("despachador detenido")
        return True

    def _bucle(self) -> None:
        while not self._parar.is_set():
            try:  # ningún error mata el hilo
                despachar_pendientes(self.db_path, self.canal, self.reloj, self._parar)
                self._parar.wait(self.intervalo_s)
            except Exception as e:
                log.error("error en el bucle del despachador: %s", type(e).__name__)
                self._parar.wait(ESPERA_TRAS_ERROR_S)
