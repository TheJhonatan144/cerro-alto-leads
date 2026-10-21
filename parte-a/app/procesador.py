"""Etapa de normalización, validación y duplicados de negocio de un evento recibido."""
import json
import uuid
from datetime import datetime, timedelta, timezone

from app import config, db
from app import normalizar as n

TIPO_SOPORTADO = "lead.created"
MOTIVO_NO_INTERPRETABLE = "No se pudo interpretar el evento"
MOTIVO_TIPO = "Tipo de evento no soportado"
MOTIVO_SIN_LEAD = "El evento no trae datos del lead"
MOTIVO_REENVIO = "Reenvío de un evento ya recibido (mismo event_id)"


def procesar_evento(con, evento_id: int, ventana_dias: int | None = None) -> dict | None:
    """Normaliza y valida un evento 'pendiente' en una sola transacción.

    Idempotente: un evento que ya no está pendiente, o que ya tiene lead_id, no se toca.
    Devuelve el estado público del evento (None si no existe).
    """
    ventana = config.duplicado_ventana_dias() if ventana_dias is None else ventana_dias
    con.execute("BEGIN IMMEDIATE")
    try:
        ev = con.execute("SELECT * FROM eventos WHERE id = ?", (evento_id,)).fetchone()
        if ev is not None and ev["estado"] == "pendiente" and ev["lead_id"] is None:
            if ev["event_id"] is not None or not _reextraer_event_id(con, ev):
                _procesar(con, ev, ventana)
            ev = con.execute("SELECT * FROM eventos WHERE id = ?", (evento_id,)).fetchone()
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return db.campos_estado(ev) if ev is not None else None


def _reextraer_event_id(con, ev) -> bool:
    """Evento guardado sin event_id: vuelve a extraerlo del cuerpo crudo.

    Si el webhook falló al extraerlo, aquí se recupera y se asigna. Si choca con el UNIQUE,
    es un reenvío: queda 'duplicado' y devuelve True. Una excepción inesperada sube y cuenta
    como error de la etapa (reintentos), nunca como rechazo. Sin event_id en el cuerpo, o
    con un cuerpo que no es un objeto, devuelve False y sigue el procesamiento normal.
    """
    try:
        datos = json.loads(ev["cuerpo_crudo"])
    except (ValueError, RecursionError):
        return False  # _procesar lo rechazará
    if not isinstance(datos, dict):
        return False
    event_id = n.normalizar_event_id(datos.get("event_id"))
    if event_id is None:
        return False  # el cuerpo realmente no trae event_id (D3)
    # Como en la recepción: decide la restricción UNIQUE, no un SELECT previo.
    cur = con.execute(
        "UPDATE OR IGNORE eventos SET event_id = ? WHERE id = ? AND event_id IS NULL",
        (event_id, ev["id"]),
    )
    if cur.rowcount == 1:
        return False
    original = con.execute("SELECT lead_id FROM eventos WHERE event_id = ?", (event_id,)).fetchone()
    con.execute(
        """UPDATE eventos SET estado = 'duplicado', duplicado_de = ?, lead_id = ?, motivo = ?
           WHERE id = ?""",
        (event_id, original["lead_id"], MOTIVO_REENVIO, ev["id"]),
    )
    return True


def _rechazar(con, ev, motivo: str) -> None:
    con.execute("UPDATE eventos SET estado = 'rechazado', motivo = ? WHERE id = ?", (motivo, ev["id"]))


def _falla(valor_crudo, normalizado) -> str | None:
    """None si el campo es válido; si no, 'ausente' o 'inválido' (sin mirar el valor más allá)."""
    if normalizado is not None:
        return None
    if valor_crudo is None or (isinstance(valor_crudo, str) and not valor_crudo.strip()):
        return "ausente"
    return "inválido"


def _procesar(con, ev, ventana: int) -> None:
    try:
        datos = json.loads(ev["cuerpo_crudo"])
    except (ValueError, RecursionError):
        datos = None
    if not isinstance(datos, dict):
        return _rechazar(con, ev, MOTIVO_NO_INTERPRETABLE)
    if datos.get("event_type") != TIPO_SOPORTADO:
        return _rechazar(con, ev, MOTIVO_TIPO)
    lead = datos.get("lead")
    if not isinstance(lead, dict):
        return _rechazar(con, ev, MOTIVO_SIN_LEAD)

    telefono = n.normalizar_telefono(lead.get("phone"))
    email = n.normalizar_email(lead.get("email"))
    falla_tel = _falla(lead.get("phone"), telefono)
    falla_email = _falla(lead.get("email"), email)
    if telefono is None and email is None:
        return _rechazar(
            con, ev, f"Sin teléfono ni correo válidos: teléfono {falla_tel}, correo {falla_email}"
        )

    advertencias = []
    if falla_tel:
        advertencias.append("telefono_ausente" if falla_tel == "ausente" else "telefono_invalido")
    if falla_email:
        advertencias.append("correo_ausente" if falla_email == "ausente" else "correo_invalido")

    nombre = n.normalizar_nombre(lead.get("full_name"))
    clave = n.clave_nombre(lead.get("full_name"))
    if not nombre:
        advertencias.append("nombre_vacio")
    mensaje = n.normalizar_texto(lead.get("message"))
    if mensaje is None:
        advertencias.append("mensaje_vacio")
    ciudad = n.normalizar_texto(lead.get("city"))
    if ciudad is None:
        advertencias.append("ciudad_ausente")

    fuente = datos.get("source") if isinstance(datos.get("source"), dict) else {}
    servicio, adv_s = n.normalizar_catalogo(
        lead.get("service_interest"), n.SERVICIOS, "servicio_ausente", "servicio_desconocido")
    plataforma, adv_p = n.normalizar_catalogo(
        fuente.get("platform"), n.PLATAFORMAS, "plataforma_ausente", "plataforma_desconocida")
    campana, adv_c = n.normalizar_catalogo(
        fuente.get("campaign"), n.CAMPANAS, "campana_ausente", "campana_desconocida")
    advertencias += [a for a in (adv_s, adv_p, adv_c) if a]
    form_id = n.normalizar_texto(fuente.get("form_id"))

    original = _buscar_original(con, ev, telefono, email, clave, ventana)
    if original is not None:
        con.execute(
            """UPDATE eventos SET estado = 'duplicado', duplicado_de = ?, lead_id = ?,
                                  telefono_e164 = ?, email = ?, motivo = ?
               WHERE id = ?""",
            (original["event_id"], original["lead_id"], telefono, email,
             f"Mismo contacto y nombre que un evento anterior ({original['received_at']})", ev["id"]),
        )
        # El duplicado no crea lead, pero su mensaje y servicio se conservan en el original.
        con.execute(
            """INSERT INTO lead_mensajes (lead_id, evento_id, recibido_at, servicio, mensaje, creado_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (original["lead_id"], ev["id"], ev["received_at"], servicio, mensaje, db.ahora_utc()),
        )
        return

    lead_id = str(uuid.uuid4())
    con.execute(
        """INSERT INTO leads (lead_id, evento_id, nombre, clave_nombre, telefono_e164, email,
                              ciudad, servicio, mensaje, plataforma, campana, form_id,
                              advertencias, creado_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (lead_id, ev["id"], nombre, clave, telefono, email, ciudad, servicio, mensaje,
         plataforma, campana, form_id, json.dumps(advertencias, ensure_ascii=False), db.ahora_utc()),
    )
    con.execute(
        "UPDATE eventos SET lead_id = ?, telefono_e164 = ?, email = ? WHERE id = ?",
        (lead_id, telefono, email, ev["id"]),
    )


def _buscar_original(con, ev, telefono, email, clave, ventana: int):
    """Evento aceptado más antiguo, anterior y dentro de la ventana, con mismo nombre y contacto.

    Solo cuentan originales con event_id, para que duplicado_de sea siempre consultable.
    """
    if not clave or (telefono is None and email is None):
        return None
    recibido = datetime.strptime(ev["received_at"], db.FORMATO_UTC).replace(tzinfo=timezone.utc)
    limite = (recibido - timedelta(days=ventana)).strftime(db.FORMATO_UTC)
    return con.execute(
        """SELECT e.event_id, e.received_at, l.lead_id
           FROM leads l JOIN eventos e ON e.id = l.evento_id
           WHERE e.id < ? AND e.received_at >= ? AND e.event_id IS NOT NULL
             AND e.estado NOT IN ('rechazado', 'duplicado')
             AND l.clave_nombre <> '' AND l.clave_nombre = ?
             AND ((? IS NOT NULL AND l.telefono_e164 = ?) OR (? IS NOT NULL AND l.email = ?))
           ORDER BY e.received_at, e.id
           LIMIT 1""",
        (ev["id"], limite, clave, telefono, telefono, email, email),
    ).fetchone()
