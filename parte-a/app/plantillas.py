"""Textos de avisos y seguimientos. Sin LLM: solo datos de la base y hechos de la ficha.

Texto plano (sin Markdown ni parse_mode). Nunca incluyen el texto libre del lead. Todo campo
que viene del lead pasa por limpiar_campo (D17): ningún valor puede añadir líneas, controles,
marcas bidireccionales ni enlaces a la estructura fija del texto.
"""
import re
import unicodedata

from app import agenda
from app import normalizar as n
from app.datos_reglas import ETIQUETAS
# Mismo patrón lineal ya auditado del enmascarado (Paso 0b): con protocolo, www. y dominios
# sin protocolo ("evil.com/login", "bit.ly/abc").
from app.enmascarar import _ENLACE as _URL_CAMPO
_CON_PROTOCOLO = re.compile(r"https?://|www\.", re.IGNORECASE)
_ETIQUETA_HTML = re.compile(r"<[^>]*>")
_TELEFONO_PERMITIDO = set("0123456789+ ()-")
MARCA_ENLACE = "[enlace]"
MAX_NOMBRE, MAX_SERVICIO, MAX_TELEFONO, MAX_CORREO, MAX_OTRO = 80, 60, 30, 120, 200


def _enlace(m: re.Match) -> str:
    """[enlace], salvo el dominio de un correo ("carla@example.com", el contacto) y un
    "dominio" que sigue con letras ("Ma.Coello": el patrón no exige fin de palabra tras el
    TLD). La URL de agenda configurada (AGENDA_URL) no pasa por aquí: va por plantilla."""
    t, i, j = m.string, m.start(), m.end()
    if _CON_PROTOCOLO.match(m.group(0)):
        return MARCA_ENLACE
    if (i > 0 and t[i - 1] == "@" and "/" not in m.group(0)) or (j < len(t) and t[j].isalnum()):
        return m.group(0)
    return MARCA_ENLACE


def limpiar_campo(texto, maxlen: int) -> str:
    """D17: NFC; sin caracteres de formato (Cf: bidireccionales, ceros de ancho) ni de control
    (Cc); los saltos de línea, tabulaciones y demás espacios pasan a un espacio; espacios
    colapsados; URLs y dominios sin protocolo -> [enlace]; recorte a maxlen con "…". Decisión
    extra: también quita etiquetas HTML (<…>) conservando su contenido, por si un canal (p. ej.
    un correo de n8n) interpretara HTML."""
    if texto is None:
        return ""
    texto = n.acotar_texto(texto if isinstance(texto, str) else str(texto))
    limpio = []
    for c in unicodedata.normalize("NFC", texto):
        categoria = unicodedata.category(c)
        if c.isspace():
            limpio.append(" ")
        elif categoria not in ("Cc", "Cf"):
            limpio.append(c)
    t = _ETIQUETA_HTML.sub("", "".join(limpio))
    t = " ".join(_URL_CAMPO.sub(_enlace, t).split())
    return t if len(t) <= maxlen else t[:maxlen - 1].rstrip() + "…"


def _telefono_limpio(valor) -> str:
    t = "".join(c for c in limpiar_campo(valor, 200) if c in _TELEFONO_PERMITIDO)
    return limpiar_campo(t, MAX_TELEFONO)


def _correo_limpio(valor) -> str:
    return limpiar_campo("".join(limpiar_campo(valor, 400).split()), MAX_CORREO)


def _servicio_limpio(valor) -> str:
    t = limpiar_campo(valor, MAX_SERVICIO)
    clave = n.clave_nombre(t)
    for canonico in n.SERVICIOS:  # catálogo de la ficha: valor canónico
        if n.clave_nombre(canonico) == clave:
            return canonico
    return t

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
    return _telefono_limpio(d.get("telefono_e164")) or _correo_limpio(d.get("email")) or "(sin contacto)"


def _campo(d: dict, clave: str, maxlen: int = MAX_OTRO, vacio: str = "") -> str:
    return limpiar_campo(d.get(clave), maxlen) or vacio


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
        f"Nombre: {_campo(d, 'nombre', MAX_NOMBRE, '(sin nombre)')}",
        f"Contacto: {_contacto(d)}",
        f"Servicio de interés: {_servicio_limpio(d.get('servicio')) or 'no indicado'}",
        f"Ruta: {_campo(d, 'ruta', 20)}. Motivo: {_campo(d, 'motivo', 300)}",
        f"Paciente: {_estado_paciente(d)}",
    ]
    citas = d.get("citas") or []
    for c in citas:
        lineas.append(
            f"Cita tentativa {c.orden}: {c.servicio_propuesto}, sede {_sede(c.sede)}, {_fecha(c)}, "
            f"{c.inicio:%H:%M} a {c.fin:%H:%M} (hora de Ecuador), {_precio(c)}. Nota: {c.nota}")
    if not citas:
        lineas.append("Cita tentativa: sin propuesta automática en los próximos 30 días; coordinar con la persona")
    lineas.append(f"Evento: {_campo(d, 'event_id', vacio='(sin event_id)')} - Lead: {_campo(d, 'lead_id')} - "
                  f"Recibido: {_campo(d, 'received_at', 40)}")
    return "\n".join(lineas)


def novedad_duplicado(d: dict) -> str:
    """D4: mensaje nuevo de un lead ya registrado con una señal caliente que el original no tenía."""
    senales = limpiar_campo(", ".join(ETIQUETAS.get(s, s) for s in d.get("senales") or []), MAX_OTRO)
    return "\n".join([
        "Actualización de un lead existente - Clínica Dental Cerro Alto",
        f"Nombre: {_campo(d, 'nombre', MAX_NOMBRE, '(sin nombre)')}",
        f"Contacto: {_contacto(d)}",
        f"Señales nuevas: {senales}",
        f"Evento nuevo: {_campo(d, 'event_id', vacio='(sin event_id)')} (recibido {_campo(d, 'received_at', 40)}) - "
        f"Lead original: {_campo(d, 'duplicado_de')} - Lead: {_campo(d, 'lead_id')}",
    ])


def _nombre_de_pila(nombre) -> str | None:
    """Primer token con letras del nombre saneado: solo letras, apóstrofe y guion, máx. 30."""
    t = limpiar_campo(nombre, MAX_NOMBRE).replace(MARCA_ENLACE, " ")
    for token in t.split():
        limpio = "".join(c for c in token if c.isalpha() or c in "'-").strip("'-")
        if any(c.isalpha() for c in limpio):
            return limpio[:MAX_NOMBRE_PILA].capitalize()
    return None


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


def incidencia(d: dict) -> str:
    """Texto de una incidencia del canal de errores: solo códigos, contadores e ids."""
    return "\n".join([
        "Incidencia del sistema de leads - Clínica Dental Cerro Alto",
        f"Código: {_campo(d, 'codigo', 40)}",
        f"Ocurrencias: {_campo(d, 'ocurrencias', 12)}",
        f"Detalle: {_campo(d, 'detalle', 200)}",
        f"Desde: {_campo(d, 'creado_at', 40)} - Última: {_campo(d, 'ultima_at', 40)}",
    ])

