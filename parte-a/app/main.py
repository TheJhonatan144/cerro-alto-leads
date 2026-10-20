"""API de recepción de leads (Bloque 1: recepción, persistencia e idempotencia)."""
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import config, db

log = logging.getLogger("leads")

# Un '+' sin codificar en la query llega como espacio: "…T10:00:00 05:00".
_OFFSET_CON_ESPACIO = re.compile(r"T.* (\d{2}(:?\d{2})?)$")

MOTIVO_NO_JSON = "El cuerpo no es JSON válido"
MOTIVO_NO_OBJETO = "El JSON recibido no es un objeto"
MOTIVO_NO_INTERPRETABLE = "No se pudo interpretar el evento"


def analizar_cuerpo(cuerpo: bytes) -> tuple[dict | None, str | None]:
    """Devuelve (objeto, None) o (None, motivo_de_rechazo). No valida el contenido."""
    try:
        datos = json.loads(cuerpo)
    except (ValueError, RecursionError):  # incluye JSONDecodeError y UTF-8 inválido
        return None, MOTIVO_NO_JSON
    if not isinstance(datos, dict):
        return None, MOTIVO_NO_OBJETO
    return datos, None


def _texto_seguro(s: str) -> str:
    # JSON admite sustitutos UTF-16 sueltos ("\ud800") que no se pueden codificar en UTF-8
    # y romperían el INSERT; se escapan como texto ("\\ud800"), de forma determinista.
    return s.encode("utf-8", "backslashreplace").decode("utf-8")


def _texto_no_vacio(valor) -> str | None:
    return _texto_seguro(valor) if isinstance(valor, str) and valor.strip() else None


def normalizar_event_id(valor) -> str | None:
    """Texto recortado o entero (no booleano) como texto; cualquier otro tipo o vacío -> None."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return str(valor)
    if isinstance(valor, str):
        return _texto_seguro(valor.strip()) or None
    return None


def parsear_desde(valor: str) -> str:
    """ISO 8601 (Z, desplazamiento o sin zona = UTC) -> formato fijo UTC. ValueError si es inválido."""
    texto = valor.strip()
    if _OFFSET_CON_ESPACIO.search(texto):
        texto = texto[: texto.rindex(" ")] + "+" + texto[texto.rindex(" ") + 1:]
    momento = datetime.fromisoformat(texto)
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(timezone.utc).strftime(db.FORMATO_UTC)


def _error(codigo: int, detalle: str, **headers) -> JSONResponse:
    return JSONResponse(status_code=codigo, content={"detail": detalle}, headers=headers or None)


def _sin_persistencia(e: Exception) -> JSONResponse:
    # Solo el tipo de error: nunca datos del lead en los logs.
    log.error("no se pudo persistir: %s", type(e).__name__)
    return _error(503, "base de datos no disponible; reintente", **{"Retry-After": "5"})


def create_app(db_path: str | Path | None = None) -> FastAPI:
    """Fábrica de la app. Servidor: uvicorn app.main:create_app --factory"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ruta = Path(db_path) if db_path else config.db_path()
    db.inicializar(ruta)

    app = FastAPI(title="Leads Clínica Dental Cerro Alto")
    app.state.db_path = ruta

    def _recibir(cuerpo: bytes) -> db.Recepcion:
        con = db.conectar(ruta)
        try:
            try:
                datos, motivo = analizar_cuerpo(cuerpo)
                return db.registrar_recepcion(
                    con,
                    cuerpo=cuerpo,
                    event_id=normalizar_event_id(datos.get("event_id")) if datos else None,
                    created_at_origen=_texto_no_vacio(datos.get("created_at")) if datos else None,
                    estado="rechazado" if motivo else "pendiente",
                    motivo=motivo,
                )
            except sqlite3.Error:
                raise
            except Exception as e:
                # Fallo al interpretar: se guarda el cuerpo crudo igualmente. Si este
                # guardado también falla, la excepción sube y la ruta responde 503.
                log.error("no se pudo interpretar el evento: %s", type(e).__name__)
                return db.registrar_recepcion(
                    con,
                    cuerpo=cuerpo,
                    event_id=None,
                    created_at_origen=None,
                    estado="rechazado",
                    motivo=MOTIVO_NO_INTERPRETABLE,
                )
        finally:
            con.close()

    def _consultar(fn, *args):
        con = db.conectar(ruta)
        try:
            return fn(con, *args)
        finally:
            con.close()

    @app.post("/webhook/leads")
    async def recibir_lead(request: Request):
        cuerpo = await request.body()
        try:
            r = await run_in_threadpool(_recibir, cuerpo)
        except Exception as e:  # SQLite o fallo del guardado de respaldo: nada persistido
            return _sin_persistencia(e)
        log.info("recepcion_id=%s event_id=%s nuevo=%s estado=%s",
                 r.recepcion_id, r.estado["event_id"], r.nuevo, r.estado["estado"])
        # 202 solo tras el COMMIT (registrar_recepcion ya volvió). Repetido: 200 + estado original.
        return JSONResponse(status_code=202 if r.nuevo else 200,
                            content={"recepcion_id": r.recepcion_id, **r.estado})

    @app.get("/leads/estado")
    def estado_leads(event_id: str | None = None, desde: str | None = None):
        if event_id is None and desde is None:
            return _error(400, "indique event_id o desde")
        if event_id is not None and desde is not None:
            return _error(400, "use solo uno de los parámetros: event_id o desde")
        try:
            if event_id is not None:
                event_id = event_id.strip()
                if not event_id:
                    return _error(400, "event_id vacío")
                estado = _consultar(db.estado_por_event_id, event_id)
                return estado if estado else _error(404, "event_id no encontrado")
            try:
                desde_utc = parsear_desde(desde)
            except (ValueError, OverflowError):  # Overflow: fuera de rango al pasar a UTC
                return _error(400, "desde debe ser una fecha ISO 8601")
            return _consultar(db.estados_desde, desde_utc)
        except sqlite3.Error as e:
            return _sin_persistencia(e)

    @app.get("/salud")
    def salud():
        return {"status": "ok"}

    return app
