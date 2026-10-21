"""Etapa enriquecer_paciente: cruza el lead con la API de pacientes (D5, D6, D7).

Idempotente: el resultado se guarda con UPSERT en 'enriquecimientos' (una fila por evento).
Nunca lanza por fallos de la API (degrada a 'no_disponible'); solo deja salir Reprogramar
mientras queden reprogramaciones, y los errores de la base, que gestiona el trabajador.
"""
import json
import logging
from dataclasses import dataclass, field

from app import db
from app import normalizar as n
from app.errores import MAX_REPROGRAMACIONES, Reprogramar, reportar_error
from app.pacientes import ApiNoDisponible, NoEncontrado

log = logging.getLogger("leads.enriquecimiento")

NOMBRE = "enriquecer_paciente"
PRESUPUESTO_S = 30.0  # la lease es de 60 s
MAX_TIPOS = 5
# Fallos que se reprograman antes de degradar (además de 429 y del limitador, que ya llegan
# como Reprogramar). Búsqueda y tratamientos: la etapa entera se repite.
CODIGOS_REPROGRAMABLES = ("forma_inesperada",)
ESPERA_REPROGRAMAR_S = 5

MOTIVOS = {
    "no_configurado": "API de pacientes no configurada",
    "timeout": "La API de pacientes no respondió a tiempo",
    "error_de_red": "No se pudo conectar con la API de pacientes",
    "error_del_servidor": "La API de pacientes respondió con un error del servidor",
    "credenciales": "La API de pacientes rechazó las credenciales",
    "solicitud_rechazada": "La API de pacientes rechazó la consulta (400)",
    "forma_inesperada": "La API de pacientes devolvió una respuesta con formato inesperado",
    "respuesta_inesperada": "La API de pacientes devolvió un código de respuesta inesperado",
    "tiempo_agotado": "Se agotó el tiempo disponible para consultar la API de pacientes",
    "limite_de_solicitudes": "Se alcanzó el límite de solicitudes de la API de pacientes",
    "limite_de_uso": "Límite de uso de la API de pacientes: pidió esperar más de 120 s",
    "respuesta_demasiado_grande": "La API de pacientes devolvió una respuesta demasiado grande",
}
MOTIVO_AMBIGUA = "Hay pacientes con el mismo contacto, pero el nombre no identifica a uno solo"
MOTIVO_INCOMPLETA = "Búsqueda incompleta: se alcanzó el tope de páginas de la API de pacientes"


@dataclass
class Resultado:
    estado: str
    coincidencia: str | None = None
    paciente_id: str | None = None
    sede_habitual: str | None = None
    ultima_visita: str | None = None
    estado_paciente: str | None = None
    tratamientos_pendientes: int | None = None
    tratamientos_completos: int | None = None
    tipos_pendientes: list = field(default_factory=list)
    advertencias: list = field(default_factory=list)
    motivo: str | None = None


def enriquecer_paciente(con, trabajo, ctx) -> None:
    ev = con.execute("SELECT estado, lead_id FROM eventos WHERE id = ?",
                     (trabajo["evento_id"],)).fetchone()
    # Solo leads aceptados y aún pendientes: nunca rechazados, duplicados ni fallidos.
    if ev is None or ev["estado"] != "pendiente" or ev["lead_id"] is None:
        return
    lead = con.execute("SELECT lead_id, telefono_e164, email, clave_nombre FROM leads WHERE lead_id = ?",
                       (ev["lead_id"],)).fetchone()
    if ctx.pacientes is None:
        resultado = Resultado("no_configurado", motivo=MOTIVOS["no_configurado"])
    else:
        try:
            resultado = _consultar(ctx.pacientes.sesion(PRESUPUESTO_S), lead)
        except Reprogramar:
            if trabajo["reprogramaciones"] < MAX_REPROGRAMACIONES:
                raise
            resultado = _no_disponible("limite_de_solicitudes", trabajo["evento_id"])
        except ApiNoDisponible as e:
            # Bloque 6.10: la API real devolvió forma_inesperada de forma intermitente. Se
            # reprograma como un 429 (sin gastar intento, hasta MAX_REPROGRAMACIONES) y solo al
            # agotarlas se degrada y se registra la incidencia, una vez.
            if e.codigo in CODIGOS_REPROGRAMABLES and trabajo["reprogramaciones"] < MAX_REPROGRAMACIONES:
                raise Reprogramar(ESPERA_REPROGRAMAR_S) from None
            resultado = _no_disponible(e.codigo, trabajo["evento_id"])
    _guardar(con, trabajo["evento_id"], lead["lead_id"], resultado)


def _no_disponible(codigo: str, evento_id) -> Resultado:
    nivel = logging.ERROR if codigo == "credenciales" else logging.WARNING
    reportar_error("api_pacientes", codigo, evento_id=evento_id, nivel=nivel)
    return Resultado("no_disponible", motivo=MOTIVOS.get(codigo, MOTIVOS["respuesta_inesperada"]))


def _consultar(sesion, lead) -> Resultado:
    candidatos, incompleta, advertencias = sesion.buscar_pacientes(lead["telefono_e164"], lead["email"])
    if incompleta:
        # Con páginas sin leer no se puede afirmar que la coincidencia sea única.
        return Resultado("ambigua", coincidencia="ambigua",
                         advertencias=advertencias + ["tope_paginas_pacientes"],
                         motivo=MOTIVO_INCOMPLETA)
    unicos = list({p.id: p for p in candidatos}.values())  # el mismo paciente en dos páginas
    if not unicos:
        return Resultado("no_paciente", coincidencia="ninguna", advertencias=advertencias)
    # D7.2: paciente solo si exactamente una candidata es compatible por nombre.
    elegido = n.elegir_por_nombre(lead["clave_nombre"], [p.clave_nombre for p in unicos])
    if elegido is None:
        return Resultado("ambigua", coincidencia="ambigua", advertencias=advertencias,
                         motivo=MOTIVO_AMBIGUA)
    p = unicos[elegido]
    resultado = Resultado("paciente", coincidencia="unica", paciente_id=p.id,
                          sede_habitual=p.sede_habitual, ultima_visita=p.ultima_visita,
                          estado_paciente=p.estado,
                          advertencias=advertencias + list(p.advertencias))
    try:
        tratamientos, incompletos, adv_t = sesion.tratamientos(p.id)
    except NoEncontrado:
        resultado.advertencias.append("tratamientos_no_disponibles")
        return resultado
    resultado.advertencias += [a for a in adv_t if a not in resultado.advertencias]
    pendientes = [t for t in tratamientos if t.estado == "pendiente"]
    if any(t.tipo is None for t in pendientes):
        resultado.advertencias.append("tipo_tratamiento_invalido")
    resultado.tipos_pendientes = list(dict.fromkeys(t.tipo for t in pendientes if t.tipo))[:MAX_TIPOS]
    if not incompletos:
        resultado.tratamientos_pendientes, resultado.tratamientos_completos = len(pendientes), 1
    else:
        resultado.advertencias.append("tope_paginas_tratamientos")
        resultado.tratamientos_completos = 0
        # Con pendientes vistos, el conteo es un mínimo válido; sin ninguno, es desconocido.
        resultado.tratamientos_pendientes = len(pendientes) if pendientes else None
    return resultado


def _guardar(con, evento_id: int, lead_id: str, r: Resultado) -> None:
    con.execute(
        """INSERT INTO enriquecimientos (evento_id, lead_id, estado_enriquecimiento, coincidencia,
                                         paciente_id, sede_habitual, ultima_visita, estado_paciente,
                                         tratamientos_pendientes, tratamientos_completos,
                                         tipos_pendientes, advertencias, motivo, consultado_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(evento_id) DO UPDATE SET
               lead_id = excluded.lead_id,
               estado_enriquecimiento = excluded.estado_enriquecimiento,
               coincidencia = excluded.coincidencia,
               paciente_id = excluded.paciente_id,
               sede_habitual = excluded.sede_habitual,
               ultima_visita = excluded.ultima_visita,
               estado_paciente = excluded.estado_paciente,
               tratamientos_pendientes = excluded.tratamientos_pendientes,
               tratamientos_completos = excluded.tratamientos_completos,
               tipos_pendientes = excluded.tipos_pendientes,
               advertencias = excluded.advertencias,
               motivo = excluded.motivo,
               consultado_at = excluded.consultado_at""",
        (evento_id, lead_id, r.estado, r.coincidencia, r.paciente_id, r.sede_habitual,
         r.ultima_visita, r.estado_paciente, r.tratamientos_pendientes, r.tratamientos_completos,
         json.dumps(r.tipos_pendientes, ensure_ascii=False),
         json.dumps(r.advertencias, ensure_ascii=False), r.motivo, db.ahora_utc()),
    )
    log.info("evento=%s enriquecimiento=%s", evento_id, r.estado)
