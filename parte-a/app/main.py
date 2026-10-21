"""API de recepción de leads: webhook idempotente, consulta de estado y trabajador."""
import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import config, db, errores
from app.normalizar import normalizar_event_id
from app.despachador import Despachador, crear_canal
from app.resumen import construir_resumen
from app.resumen import parsear_desde as parsear_desde_con_zona
from app.trabajador import Trabajador

log = logging.getLogger("leads")

# Un '+' sin codificar en la query llega como espacio: "…T10:00:00 05:00".
_OFFSET_CON_ESPACIO = re.compile(r"T.* (\d{2}(:?\d{2})?)$")

MOTIVO_NO_JSON = "El cuerpo no es JSON válido"
MOTIVO_NO_OBJETO = "El JSON recibido no es un objeto"


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


class CuerpoDemasiadoGrande(Exception):
    def __init__(self, tamano: str):
        super().__init__(tamano)
        self.tamano = tamano  # texto para el log: "5000" o "más de 4096"


class ContentLengthInvalido(Exception):
    pass


MAX_DIGITOS_CONTENT_LENGTH = 12  # más dígitos ya supera cualquier tope; evita convertir enteros enormes


async def leer_cuerpo_acotado(request: Request, maximo: int) -> bytes:
    """D16: lee el cuerpo sin pasar de 'maximo' bytes.

    Con Content-Length mayor que el tope se rechaza sin leer nada. Sin Content-Length
    (chunked), o si el declarado es menor que el real, se lee en streaming y se aborta en
    cuanto se pasa el tope.
    """
    declarado = request.headers.get("content-length")
    if declarado is not None:
        texto = declarado.strip()
        if not texto or not (texto.isascii() and texto.isdigit()):
            raise ContentLengthInvalido()
        if len(texto) > MAX_DIGITOS_CONTENT_LENGTH or int(texto) > maximo:
            raise CuerpoDemasiadoGrande(texto if len(texto) <= MAX_DIGITOS_CONTENT_LENGTH else f"más de {maximo}")
    cuerpo = bytearray()
    async for trozo in request.stream():
        cuerpo += trozo
        if len(cuerpo) > maximo:
            raise CuerpoDemasiadoGrande(f"más de {maximo}")
    return bytes(cuerpo)


def _sin_persistencia(e: Exception) -> JSONResponse:
    # Solo el tipo de error: nunca datos del lead en los logs.
    log.error("no se pudo persistir: %s", type(e).__name__)
    return _error(503, "base de datos no disponible; reintente", **{"Retry-After": "5"})


def create_app(db_path: str | Path | None = None) -> FastAPI:
    """Fábrica de la app. Servidor: uvicorn app.main:create_app --factory

    Carga .env y valida toda la configuración aquí: un valor inválido impide arrancar.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config.cargar_env()
    cfg = config.cargar_config()
    if not cfg.pacientes_configurada:
        log.warning("API de pacientes no configurada (API_PACIENTES_URL o API_PACIENTES_TOKEN "
                    "vacías): los leads quedarán con enriquecimiento=no_configurado")
    if cfg.llm_proveedor == "none":
        log.warning("LLM_PROVIDER=none: clasificación solo con reglas")
    # Aviso único con los canales activos (solo nombres: nunca tokens, URLs ni secretos).
    if cfg.avisos_canales == ("archivo",):
        log.warning("canales de aviso: archivo; avisos por canal archivo (%s): recepción no recibe "
                    "notificaciones en tiempo real", cfg.avisos_archivo.name)
    else:
        log.warning("canales de aviso: %s; canales de errores: %s", ", ".join(cfg.avisos_canales),
                    ", ".join(cfg.errores_canales))
    ruta = Path(db_path) if db_path else cfg.db_path
    db.inicializar(ruta)
    errores.configurar(ruta, cfg.errores_ventana_s)  # desde aquí, reportar_error escribe incidencias

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # WORKER_ACTIVO controla los dos hilos de fondo: trabajador y despachador de avisos.
        hilos: dict = {}
        trabajador = (Trabajador(ruta, cfg, hilos=lambda: {"despachador": hilos["despachador"].estado()})
                      if cfg.worker_activo else None)
        despacho = (Despachador(ruta, crear_canal(cfg), canal_errores=crear_canal(cfg, "errores"), cfg=cfg,
                                hilos=lambda: {"trabajador": hilos["trabajador"].estado()})
                    if cfg.worker_activo else None)
        hilos.update(trabajador=trabajador, despachador=despacho)
        app.state.trabajador, app.state.despachador = trabajador, despacho
        for hilo in (trabajador, despacho):
            if hilo:
                hilo.iniciar()
        try:
            yield
        finally:
            for hilo in (trabajador, despacho):
                if hilo:
                    await run_in_threadpool(hilo.detener)

    app = FastAPI(title="Leads Clínica Dental Cerro Alto", lifespan=lifespan)
    app.state.db_path = ruta
    app.state.config = cfg

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
                    max_intentos=cfg.trabajo_max_intentos,
                )
            except sqlite3.Error:
                raise
            except Exception as e:
                # Fallo al interpretar: se guarda el cuerpo crudo como 'pendiente' con su
                # trabajo, y el procesador lo reintenta por su cuenta. Si este guardado
                # también falla, la excepción sube y la ruta responde 503.
                log.error("no se pudo interpretar el evento: %s", type(e).__name__)
                return db.registrar_recepcion(
                    con,
                    cuerpo=cuerpo,
                    event_id=None,
                    created_at_origen=None,
                    estado="pendiente",
                    motivo=None,
                    max_intentos=cfg.trabajo_max_intentos,
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
        try:
            cuerpo = await leer_cuerpo_acotado(request, cfg.webhook_max_bytes)
        except CuerpoDemasiadoGrande as e:
            # No se guarda nada ni se repite el cuerpo: solo el tamaño, nunca el contenido.
            log.warning("cuerpo rechazado por tamaño: %s bytes (máximo %d)", e.tamano, cfg.webhook_max_bytes)
            return _error(413, f"el cuerpo supera el máximo de {cfg.webhook_max_bytes} bytes")
        except ContentLengthInvalido:
            log.warning("cabecera Content-Length inválida")
            return _error(400, "Content-Length inválido")
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

    @app.get("/leads/resumen")
    def resumen_leads(desde: str | None = None):
        """D21: resumen operativo sin datos personales. Sin autenticación (como /leads/estado)."""
        try:
            momento = parsear_desde_con_zona(desde) if desde is not None else None
        except ValueError:
            return _error(400, "desde debe ser una fecha ISO 8601 con zona horaria")
        hilos = {nombre: (h.vivo() if h is not None else None)
                 for nombre, h in (("trabajador", getattr(app.state, "trabajador", None)),
                                   ("despachador", getattr(app.state, "despachador", None)))}
        try:
            return _consultar(lambda con: construir_resumen(con, datetime.now(timezone.utc), momento,
                                                            pendiente_antiguo_s=cfg.pendiente_antiguo_s,
                                                            hilos=hilos))
        except sqlite3.Error as e:
            return _sin_persistencia(e)

    @app.get("/salud")
    def salud():
        return {"status": "ok"}

    return app
