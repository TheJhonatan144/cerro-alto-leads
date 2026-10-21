"""Textos de avisos y seguimientos. Sin LLM: solo datos de la base y hechos de la ficha.

Texto plano (sin Markdown ni parse_mode). Nunca incluyen el texto libre del lead.
"""
from app import agenda
from app.datos_reglas import ETIQUETAS

MAX_SEGUIMIENTO = 499  # el seguimiento debe tener menos de 500 caracteres
MAX_NOMBRE_PILA = 30
DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre")
ESTADO_PACIENTE = {
    "no_paciente": "no",
    "ambigua": "coincidencia ambigua, sin confirmar",
}


def _sede(sede: str) -> str:
    return sede.capitalize()


def _fecha(c: agenda.Propuesta) -> str:
    i = c.inicio
    return f"{DIAS[i.weekday()]} {i.day} de {MESES[i.month - 1]}"


def _precio(c: agenda.Propuesta) -> str:
    return "sin costo" if c.precio_usd == 0 else f"USD {c.precio_usd}"


def _contacto(d: dict) -> str:
    return d.get("telefono_e164") or d.get("email") or "(sin contacto)"


def _estado_paciente(d: dict) -> str:
    estado = d.get("estado_enriquecimiento")
    if estado == "paciente":
        pendientes = d.get("tratamientos_pendientes")
        return "sí, con tratamiento pendiente" if pendientes else "sí"
    return ESTADO_PACIENTE.get(estado, "sin verificar")


def aviso_caliente(d: dict) -> str:
    """D11: aviso a recepción de un lead caliente. Sin el texto libre del lead ni detalle clínico."""
    lineas = [
        "Lead caliente - Clínica Dental Cerro Alto",
        f"Nombre: {d.get('nombre') or '(sin nombre)'}",
        f"Contacto: {_contacto(d)}",
        f"Servicio de interés: {d.get('servicio') or 'no indicado'}",
        f"Ruta: {d.get('ruta')}. Motivo: {d.get('motivo')}",
        f"Paciente: {_estado_paciente(d)}",
    ]
    citas = d.get("citas") or []
    for c in citas:
        lineas.append(
            f"Cita tentativa {c.orden}: {c.servicio_propuesto}, sede {_sede(c.sede)}, {_fecha(c)}, "
            f"{c.inicio:%H:%M} a {c.fin:%H:%M} (hora de Ecuador), {_precio(c)}. Nota: {c.nota}")
    if not citas:
        lineas.append("Cita tentativa: sin propuesta automática en los próximos 30 días; coordinar con la persona")
    lineas.append(f"Evento: {d.get('event_id') or '(sin event_id)'} - Lead: {d.get('lead_id')} - "
                  f"Recibido: {d.get('received_at')}")
    return "\n".join(lineas)


def novedad_duplicado(d: dict) -> str:
    """D4: mensaje nuevo de un lead ya registrado con una señal caliente que el original no tenía."""
    senales = ", ".join(ETIQUETAS.get(s, s) for s in d.get("senales") or [])
    return "\n".join([
        "Actualización de un lead existente - Clínica Dental Cerro Alto",
        f"Nombre: {d.get('nombre') or '(sin nombre)'}",
        f"Contacto: {_contacto(d)}",
        f"Señales nuevas: {senales}",
        f"Evento nuevo: {d.get('event_id') or '(sin event_id)'} (recibido {d.get('received_at')}) - "
        f"Lead original: {d.get('duplicado_de')} - Lead: {d.get('lead_id')}",
    ])


def _nombre_de_pila(nombre) -> str | None:
    palabras = nombre.split() if isinstance(nombre, str) else []
    return palabras[0][:MAX_NOMBRE_PILA].capitalize() if palabras else None


def _hecho_del_servicio(servicio, c: agenda.Propuesta | None) -> tuple[str, str]:
    """(frase con hechos de la ficha, frase de sedes). Sin descuentos salvo P2."""
    clave_servicio = agenda.n.clave_nombre(servicio) if isinstance(servicio, str) else ""
    clave_cita, sede_exigida, _ = agenda.SERVICIO_A_CITA.get(clave_servicio, agenda.POR_DEFECTO)
    tipo = agenda.CITAS[clave_cita]
    if tipo.clave == "limpieza_dental":
        hecho = f"La {tipo.nombre.lower()} dura {tipo.duracion_min} minutos y cuesta USD {tipo.precio_usd}."
    elif tipo.clave == "valoracion_ortodoncia":
        p2 = c is not None and c.inicio.date() <= agenda.P2_VIGENTE_HASTA
        precio = (f"es sin costo de lunes a viernes y los sábados cuesta USD {tipo.precio_usd}" if p2
                  else f"cuesta USD {tipo.precio_usd}")
        hecho = f"El primer paso es una {tipo.nombre.lower()} de {tipo.duracion_min} minutos, que {precio}."
    else:
        hecho = f"El primer paso es una {tipo.nombre.lower()} de {tipo.duracion_min} minutos, que cuesta USD {tipo.precio_usd}."
    if clave_servicio in ("blanqueamiento", "combo limpieza + blanqueamiento"):
        hecho = "Para el blanqueamiento, " + hecho[0].lower() + hecho[1:]
    if sede_exigida == "valle":
        return "Los alineadores se hacen en nuestra sede Valle. " + hecho, ""
    return hecho, "Atendemos en nuestras sedes Norte y Valle."


def seguimiento_tibio(nombre, servicio, citas, agenda_url=None) -> str:
    """Primer mensaje de seguimiento para un lead tibio, estilo WhatsApp, de tú."""
    pila = _nombre_de_pila(nombre)
    saludo = f"Hola {pila}, gracias por escribir a la Clínica Dental Cerro Alto." if pila else \
        "Hola, gracias por escribir a la Clínica Dental Cerro Alto."
    c = citas[0] if citas else None
    hecho, sedes = _hecho_del_servicio(servicio, c)
    if c is not None:
        oferta = (f"Te puedo proponer el {_fecha(c)} a las {c.inicio:%H:%M} en la sede {_sede(c.sede)} "
                  "(por confirmar). ¿Te queda bien o prefieres otro día?")
    else:
        oferta = "¿Qué día te queda mejor para agendar?"
    enlace = f"También puedes agendar aquí: {agenda_url}" if agenda_url else ""
    # Por prioridad: si no cabe en 500 caracteres, se quita primero la frase de sedes y
    # después el enlace (el saludo, los hechos y la invitación siempre van).
    for partes in ([saludo, hecho, sedes, oferta, enlace], [saludo, hecho, oferta, enlace],
                   [saludo, hecho, oferta]):
        texto = " ".join(p for p in partes if p)
        if len(texto) <= MAX_SEGUIMIENTO:
            return texto
    return texto[:MAX_SEGUIMIENTO]
