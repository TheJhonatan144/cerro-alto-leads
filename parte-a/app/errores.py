"""Señales compartidas entre etapas y trabajador, y el manejador central de errores (D19).

reportar_error escribe una incidencia en la base (o suma una ocurrencia a una igual dentro
de la ventana) con una conexión propia y un timeout de 1 s. NUNCA lanza ni bloquea al
llamador: si la base falla, solo deja una línea en el log. El detalle admite solo códigos,
contadores e identificadores de evento: nunca texto libre ni datos personales. Si el sistema
aún no está configurado (tests, CLI), solo registra en el log. El despachador envía las
incidencias por ERRORES_CANAL.
"""
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("leads.errores")

# Reprogramaciones por trabajo (cupo local agotado o 429). Después, la etapa degrada (D5).
MAX_REPROGRAMACIONES = 3
# Tope de espera de una reprogramación, también para Retry-After.
REPROGRAMAR_MAX_S = 120

MAX_DETALLE = 200
TIMEOUT_BASE_S = 1.0
FORMATO_UTC = "%Y-%m-%dT%H:%M:%S.%fZ"
_CODIGO = re.compile(r"[a-z0-9_]{1,40}")
_CLAVE = re.compile(r"[a-z0-9_.:-]{1,40}")
_NIVELES = (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL)

_destino: dict = {"ruta": None, "ventana_s": 900}
_cerrojo = threading.Lock()


class Reprogramar(Exception):
    """La etapa no puede seguir ahora pero sí más tarde: el trabajador la reprograma sin
    consumir intento y sin esperar dentro de la etapa."""

    def __init__(self, segundos: float):
        super().__init__(f"reprogramar en {segundos} s")
        self.segundos = max(1.0, min(float(segundos), REPROGRAMAR_MAX_S))


def configurar(db_path, ventana_s: int) -> None:
    """Activa el registro de incidencias en la base (lo llama create_app)."""
    with _cerrojo:
        _destino["ruta"], _destino["ventana_s"] = Path(db_path), int(ventana_s)


def desconfigurar() -> None:
    with _cerrojo:
        _destino["ruta"] = None


def _seguro(valor, patron: re.Pattern, defecto: str) -> str:
    return valor if isinstance(valor, str) and patron.fullmatch(valor) else defecto


def _entero(valor) -> int | None:
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else None


def _detalle(origen: str, evento_id, datos) -> str:
    """Solo códigos, contadores e ids de evento. Cualquier otro valor se descarta."""
    from app.plantillas import limpiar_campo  # perezoso: evita ciclos de importación
    partes = [f"origen={origen}"]
    if _entero(evento_id) is not None:
        partes.append(f"evento={evento_id}")
    if isinstance(datos, dict):
        for k, v in datos.items():
            if isinstance(k, str) and _CODIGO.fullmatch(k) and _entero(v) is not None:
                partes.append(f"{k}={v}")
    return limpiar_campo(" ".join(partes), MAX_DETALLE)


def _txt(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime(FORMATO_UTC)


def _registrar(ruta: Path, ventana_s: int, codigo: str, clave: str, detalle: str) -> None:
    # mode=rw: nunca crea un archivo de base vacío por error.
    con = sqlite3.connect(Path(ruta).resolve().as_uri() + "?mode=rw", uri=True,
                          timeout=TIMEOUT_BASE_S, isolation_level=None)
    try:
        con.execute(f"PRAGMA busy_timeout = {int(TIMEOUT_BASE_S * 1000)}")
        ahora = datetime.now(timezone.utc)
        t, limite = _txt(ahora), _txt(ahora - timedelta(seconds=ventana_s))
        con.execute("BEGIN IMMEDIATE")
        try:
            # Una pendiente igual (aunque sea antigua) o una ya enviada dentro de la ventana:
            # se suma la ocurrencia y no se crea otra (no se repite el aviso).
            fila = con.execute(
                """SELECT id FROM incidencias WHERE codigo = ? AND clave = ?
                   AND (estado = 'pendiente' OR creado_at >= ?) ORDER BY id DESC LIMIT 1""",
                (codigo, clave, limite)).fetchone()
            if fila is not None:
                con.execute("UPDATE incidencias SET ocurrencias = ocurrencias + 1, ultima_at = ?, detalle = ? "
                            "WHERE id = ?", (t, detalle, fila[0]))
            else:
                con.execute(
                    """INSERT INTO incidencias (codigo, clave, detalle, ocurrencias, creado_at, ultima_at,
                                                estado, intentos, proximo_intento)
                       VALUES (?, ?, ?, 1, ?, ?, 'pendiente', 0, ?)""", (codigo, clave, detalle, t, t, t))
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def reportar_error(origen: str, codigo: str, *, evento_id=None, nivel: int = logging.WARNING,
                   clave: str | None = None, datos: dict | None = None) -> None:
    """Único punto del canal de errores. Nunca lanza ni bloquea más de ~1 s.

    clave agrupa la deduplicación (por defecto, el origen); datos admite solo contadores
    enteros con nombres de código. Nunca recibe texto libre ni datos personales.
    """
    try:
        origen_ok = _seguro(origen, _CODIGO, "desconocido")
        codigo_ok = _seguro(codigo, _CODIGO, "codigo_invalido")
        clave_ok = origen_ok if clave is None else _seguro(clave, _CLAVE, origen_ok)
        detalle = _detalle(origen_ok, evento_id, datos)
        log.log(nivel if nivel in _NIVELES else logging.WARNING,
                "error de integración origen=%s codigo=%s evento=%s", origen_ok, codigo_ok, _entero(evento_id))
        with _cerrojo:
            ruta, ventana_s = _destino["ruta"], _destino["ventana_s"]
        if ruta is not None:
            _registrar(ruta, ventana_s, codigo_ok, clave_ok, detalle)
    except Exception as e:
        try:
            log.error("no se pudo registrar la incidencia: %s", type(e).__name__)
        except Exception:
            pass
