"""Etapa clasificar_lead: reglas deterministas + LLM opcional, y cierre del evento.

- Ignora eventos rechazados, fallidos o ya procesados. Nunca deja el evento sin ruta: si el
  LLM falla, clasifica solo con reglas.
- Guarda la clasificación; el cierre del evento ('procesado' con su ruta) lo hace la etapa
  siguiente, enrutar_lead, junto con su aviso o seguimiento (D15).
- Duplicados (D4): su mensaje nuevo se revisa solo con reglas; si trae una señal caliente que
  el original no tenía, se registra una novedad y su aviso en la misma transacción. El estado
  y la ruta del duplicado no cambian.
- No guarda el mensaje (ni enmascarado) ni la respuesta cruda del LLM.
"""
import json
import logging
from datetime import datetime, timezone

from app import db, despachador, reglas
from app.clasificacion import decidir_ruta, validar_salida_llm
from app.enmascarar import contenido_para_llm
from app.errores import MAX_REPROGRAMACIONES, Reprogramar, reportar_error
from app.llm import construir_mensaje_usuario
from app.pacientes import ApiNoDisponible

log = logging.getLogger("leads.clasificacion")

NOMBRE = "clasificar_lead"
PRESUPUESTO_S = 20.0
SENALES_NOVEDAD = ("urgencia_clinica", "pide_agendar_o_llamada")

FALLOS = {
    "timeout": "el LLM no respondió a tiempo",
    "error_de_red": "no se pudo conectar con el LLM",
    "error_del_servidor": "el LLM respondió con un error del servidor",
    "credenciales": "el LLM rechazó las credenciales",
    "solicitud_rechazada": "el LLM rechazó la petición",
    "forma_inesperada": "la respuesta del LLM tenía un formato inesperado",
    "respuesta_inesperada": "el LLM devolvió un código de respuesta inesperado",
    "respuesta_demasiado_grande": "la respuesta del LLM era demasiado grande",
    "salida_invalida": "la salida del LLM no cumplió el esquema",
    "tiempo_agotado": "se agotó el tiempo para consultar el LLM",
    "limite_de_uso": "el LLM pidió esperar más de 60 s",
    "limite_de_solicitudes": "se alcanzó el límite de solicitudes del LLM",
    "rechazo_del_modelo": "el LLM se negó a clasificar",
    "excepcion_inesperada": "error inesperado al consultar el LLM",
}


def _contactos_crudos(cuerpo: bytes):
    """Teléfono y correo tal como llegaron (para detectar contactos falsos)."""
    try:
        lead = json.loads(cuerpo).get("lead")
    except (ValueError, RecursionError, AttributeError):
        return None, None
    if not isinstance(lead, dict):
        return None, None
    return lead.get("phone"), lead.get("email")


def clasificar_lead(con, trabajo, ctx) -> None:
    ev = con.execute("SELECT id, estado, lead_id, duplicado_de, cuerpo_crudo FROM eventos WHERE id = ?",
                     (trabajo["evento_id"],)).fetchone()
    if ev is None:
        return
    if ev["estado"] == "duplicado":
        return _revisar_duplicado(con, ev, ctx)
    if ev["estado"] != "pendiente" or ev["lead_id"] is None:
        return  # rechazado, fallido o ya procesado
    lead = con.execute("SELECT * FROM leads WHERE lead_id = ?", (ev["lead_id"],)).fetchone()
    enr = con.execute("SELECT coincidencia, tratamientos_pendientes FROM enriquecimientos "
                      "WHERE evento_id = ?", (ev["id"],)).fetchone()
    telefono, email = _contactos_crudos(ev["cuerpo_crudo"])
    resultado = reglas.evaluar(lead["mensaje"], servicio=lead["servicio"], campana=lead["campana"],
                               enriquecimiento=enr, telefono=telefono or lead["telefono_e164"],
                               email=email or lead["email"])

    salida, fallo = None, None
    if ctx.llm is not None:
        try:
            salida = _consultar_llm(ctx, lead)
        except Reprogramar:
            if trabajo["reprogramaciones"] < MAX_REPROGRAMACIONES:
                raise
            fallo = "limite_de_solicitudes"
        except ApiNoDisponible as e:
            fallo = e.codigo
        except Exception as e:
            # Nunca consume reintentos del trabajador ni deja el evento sin ruta. Solo el
            # tipo: el mensaje de la excepción podría llevar datos del lead.
            log.error("evento=%s excepción inesperada en la rama del LLM: %s", ev["id"], type(e).__name__)
            fallo = "excepcion_inesperada"
        if fallo:
            nivel = logging.ERROR if fallo == "credenciales" else logging.WARNING
            reportar_error("llm", fallo, evento_id=ev["id"], nivel=nivel)

    decision = decidir_ruta(resultado, salida, ctx.cfg.llm_confianza_basura)
    motivo = decision.motivo
    if fallo:
        motivo += f" (clasificado solo con reglas: {FALLOS.get(fallo, 'fallo del LLM')})"
    _guardar(con, ev, lead, decision, motivo, salida, fallo, ctx)


def _consultar_llm(ctx, lead):
    # Solo el mensaje enmascarado, el servicio y la campaña salen hacia el LLM.
    contenido = contenido_para_llm(lead["mensaje"], servicio=lead["servicio"],
                                   campana=lead["campana"], nombre=lead["nombre"])
    return ctx.llm.clasificar(ctx.prompt.texto, construir_mensaje_usuario(contenido),
                              ctx.llm.presupuesto(PRESUPUESTO_S), validar_salida_llm)


def _guardar(con, ev, lead, decision, motivo, salida, fallo, ctx) -> None:
    usado_llm = ctx.llm is not None
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            """INSERT INTO clasificaciones (evento_id, lead_id, ruta, fuente, senales, intencion,
                   urgencia, presupuesto_estimado_usd, siguiente_accion, confianza, motivo,
                   prompt_version, prompt_hash, proveedor, modelo, fallo_llm, clasificado_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(evento_id) DO UPDATE SET
                   lead_id = excluded.lead_id, ruta = excluded.ruta, fuente = excluded.fuente,
                   senales = excluded.senales, intencion = excluded.intencion,
                   urgencia = excluded.urgencia,
                   presupuesto_estimado_usd = excluded.presupuesto_estimado_usd,
                   siguiente_accion = excluded.siguiente_accion, confianza = excluded.confianza,
                   motivo = excluded.motivo, prompt_version = excluded.prompt_version,
                   prompt_hash = excluded.prompt_hash, proveedor = excluded.proveedor,
                   modelo = excluded.modelo, fallo_llm = excluded.fallo_llm,
                   clasificado_at = excluded.clasificado_at""",
            (ev["id"], lead["lead_id"], decision.ruta, "llm" if salida else "reglas",
             json.dumps(list(decision.senales)),
             salida.intencion if salida else None, salida.urgencia if salida else None,
             salida.presupuesto_estimado_usd if salida else None, decision.siguiente_accion,
             salida.confianza if salida else None, motivo,
             ctx.prompt.version if usado_llm else None, ctx.prompt.hash if usado_llm else None,
             ctx.cfg.llm_proveedor if usado_llm else None, ctx.cfg.llm_modelo if usado_llm else None,
             fallo, db.ahora_utc()),
        )
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    log.info("evento=%s ruta=%s fuente=%s", ev["id"], decision.ruta, "llm" if salida else "reglas")


def _revisar_duplicado(con, ev, ctx) -> None:
    nuevo = con.execute("SELECT lead_id, mensaje, servicio FROM lead_mensajes WHERE evento_id = ?",
                        (ev["id"],)).fetchone()
    if nuevo is None:
        return  # reenvío por event_id: no trae mensaje nuevo
    original = con.execute("SELECT mensaje, servicio, campana FROM leads WHERE lead_id = ?",
                           (nuevo["lead_id"],)).fetchone()
    senales_nuevo = set(reglas.evaluar(nuevo["mensaje"], servicio=nuevo["servicio"]).senales)
    senales_original = set(reglas.evaluar(original["mensaje"], servicio=original["servicio"],
                                          campana=original["campana"]).senales) if original else set()
    novedad = [s for s in SENALES_NOVEDAD if s in senales_nuevo and s not in senales_original]
    if not novedad:
        return
    recibido = con.execute("SELECT received_at FROM eventos WHERE id = ?", (ev["id"],)).fetchone()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            """INSERT INTO novedades_duplicado (evento_id, duplicado_de, lead_id, senales, creada_at)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT(evento_id) DO NOTHING""",
            (ev["id"], ev["duplicado_de"], nuevo["lead_id"], json.dumps(novedad), db.ahora_utc()),
        )
        # El aviso de la novedad, en la misma transacción (outbox).
        despachador.crear_aviso(con, ev["id"], "novedad_duplicado", recibido["received_at"], ctx.cfg,
                                ctx.reloj() if getattr(ctx, "reloj", None) else datetime.now(timezone.utc))
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    log.info("evento=%s novedad de duplicado: %s", ev["id"], ",".join(novedad))
