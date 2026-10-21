"""Señales deterministas de clasificación. Función pura: sin red ni base de datos.

El texto del lead es DATO: aquí solo se buscan patrones, nunca se interpreta como orden.
"""
import re
from dataclasses import dataclass

from app import datos_reglas as d
from app import normalizar as n

SENALES_CALIENTES = ("urgencia_clinica", "pide_agendar_o_llamada", "alto_valor_con_intencion",
                     "paciente_con_pendiente")
# Positivas: impiden declarar basura (además de las calientes, el interés real).
SENALES_POSITIVAS = SENALES_CALIENTES + ("interes_en_servicio",)

# Cláusulas (heurística, Bloque 4.2): el alcance de una negación se corta con punto, punto y
# coma, signos de pregunta o exclamación, adversativas ("sin dolor, pero hinchado" sí es
# urgencia) o una coma seguida de un inicio claro de cláusula nueva ("no puedo esperar,
# tengo dolor"). Una coma dentro de una enumeración NO corta ("sin dolor, hinchazón ni fiebre").
_FRASES = re.compile(
    r"[.!?;\n¿¡]+"
    r"|(?<![a-z0-9])(?:pero|aunque|sin embargo|sino)(?![a-z0-9])"
    r"|,(?=\s*(?:se me|me|tengo|estoy|siento|hay|ya)(?![a-z0-9]))"
)
_NEGADOR = re.compile(r"(?<![a-z0-9])(?:" + "|".join(sorted(d.NEGADORES)) + r")(?![a-z0-9])")
_SEPARADOR_LISTA = re.compile(r",|(?<![a-z0-9])(?:ni|y|o)(?![a-z0-9])")
_LISTA_NEGABLE = re.compile(r",|(?<![a-z0-9])ni(?![a-z0-9])")
MAX_PALABRAS_ELEMENTO = 4  # elementos cortos: "hinchazón", "tengo dolor de muela"
_PALABRA = re.compile(r"[a-z0-9]+")
# El dominio sin protocolo solo empieza al inicio de un tramo (tiempo lineal, Bloque 4.2).
_URL = re.compile(r"(?:https?://|www\.)\S+|(?<![a-z0-9.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|ec|io|ly|me|co|info|biz|xyz|example)/\S*")


def _patron(lista) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(lista) + r")(?![a-z0-9])")


_DOLOR_NO_DENTAL = _patron(d.DOLOR_NO_DENTAL)
_URGENCIA = _patron(d.URGENCIA)
_FIEBRE = _patron([d.FIEBRE])
_CONTEXTO_DENTAL = _patron([d.CONTEXTO_DENTAL])
_AGENDAR = _patron(d.AGENDAR)
_INTENCION = _patron(d.INTENCION_ALTO_VALOR)
_INTERES = _patron(d.INTERES)
_SPAM = _patron(d.SPAM_FUERTE)
_INYECCION = _patron(d.INYECCION)
_SECUENCIAS = "01234567890123456789"


@dataclass(frozen=True)
class ResultadoReglas:
    senales: tuple[str, ...]
    piso: str | None  # "caliente" si hay alguna señal caliente
    spam: bool
    contacto_falso: bool

    @property
    def positivas(self) -> bool:
        return any(s in SENALES_POSITIVAS for s in self.senales)

    @property
    def inyeccion(self) -> bool:
        return "posible_inyeccion" in self.senales


def _frases(texto: str) -> list[str]:
    return [f.strip() for f in _FRASES.split(texto) if f.strip()]


def _en_lista_negada(previo: str) -> bool:
    """La señal es un elemento de una enumeración que empieza con una negación:
    "sin dolor, hinchazón, inflamación, fiebre". Exige al menos una coma o "ni" entre la
    negación y la señal, y elementos de como mucho MAX_PALABRAS_ELEMENTO palabras."""
    negadores = list(_NEGADOR.finditer(previo))
    if not negadores:
        return False
    tramo = previo[negadores[-1].end():]
    if not _LISTA_NEGABLE.search(tramo):
        return False
    return all(len(_PALABRA.findall(e)) <= MAX_PALABRAS_ELEMENTO for e in _SEPARADOR_LISTA.split(tramo))


def _negada(frase: str, inicio: int, listas: bool = False) -> bool:
    previas = _PALABRA.findall(frase[:inicio])[-3:]
    if any(p in d.NEGADORES for p in previas):
        return True
    return listas and _en_lista_negada(frase[:inicio])


def _hay(patron: re.Pattern, frases: list[str], negable: bool = True, listas: bool = False) -> bool:
    for frase in frases:
        for m in patron.finditer(frase):
            if not (negable and _negada(frase, m.start(), listas)):
                return True
    return False


def _urgencia(frases: list[str], texto: str) -> bool:
    limpias = [_DOLOR_NO_DENTAL.sub(" ", f) for f in frases]
    # Solo para síntomas: la enumeración negada no anula otras señales ("no tengo dolor,
    # fiebre y quiero una cita" sigue pidiendo cita).
    if _hay(_URGENCIA, limpias, listas=True):
        return True
    return _hay(_FIEBRE, limpias, listas=True) and bool(_CONTEXTO_DENTAL.search(_DOLOR_NO_DENTAL.sub(" ", texto)))


def _telefono_falso(telefono) -> bool:
    if isinstance(telefono, bool) or not isinstance(telefono, (str, int)):
        return False
    digitos = re.sub(r"\D", "", str(telefono))
    if len(digitos) < 7:
        return False
    significativos = digitos[-9:]
    return (len(set(significativos)) == 1 or significativos in _SECUENCIAS
            or significativos in _SECUENCIAS[::-1])


def _email_falso(email) -> bool:
    if not isinstance(email, str) or "@" not in email:
        return False
    return email.strip().lower().rsplit("@", 1)[1] in d.DOMINIOS_DESECHABLES


def _campo(enr, clave):
    try:
        return enr[clave]
    except (KeyError, IndexError, TypeError):
        return None


def evaluar(mensaje, *, servicio=None, campana=None, enriquecimiento=None,
            telefono=None, email=None) -> ResultadoReglas:
    """Señales sobre el mensaje, el servicio, la campaña, el enriquecimiento y los contactos."""
    # Tope antes de cualquier expresión regular (Bloque 4.2).
    texto = n.clave_nombre(n.acotar_texto(mensaje)) if isinstance(mensaje, str) else ""
    frases = _frases(texto)
    clave_servicio = n.clave_nombre(servicio) if isinstance(servicio, str) else ""
    clave_campana = n.clave_nombre(campana) if isinstance(campana, str) else ""
    senales = []

    if _urgencia(frases, texto):
        senales.append("urgencia_clinica")
    agendar = _hay(_AGENDAR, frases)
    if agendar:
        senales.append("pide_agendar_o_llamada")
    # D8: el servicio (o la campaña, si no hay servicio útil) de alto valor no basta por sí
    # solo; hace falta una señal de intención en el mensaje.
    alto_valor = clave_servicio in d.SERVICIOS_ALTO_VALOR or (
        clave_servicio in ("", "otro") and any(s in clave_campana for s in d.SERVICIOS_ALTO_VALOR))
    if alto_valor and (agendar or _hay(_INTENCION, frases)):
        senales.append("alto_valor_con_intencion")
    pendientes = _campo(enriquecimiento, "tratamientos_pendientes")
    if (_campo(enriquecimiento, "coincidencia") == "unica" and isinstance(pendientes, int)
            and pendientes > 0):
        senales.append("paciente_con_pendiente")
    if _hay(_INTERES, frases, negable=False):
        senales.append("interes_en_servicio")
    puntos = (d.PUNTOS_SPAM_FUERTE * len(_SPAM.findall(texto))
              + (d.PUNTOS_ENLACE if _URL.search(texto) else 0))
    spam = puntos >= d.UMBRAL_SPAM
    if spam:
        senales.append("spam")
    falso = _telefono_falso(telefono) and _email_falso(email)
    if falso:
        senales.append("contacto_falso")
    if _INYECCION.search(texto):
        senales.append("posible_inyeccion")

    piso = "caliente" if any(s in SENALES_CALIENTES for s in senales) else None
    return ResultadoReglas(senales=tuple(senales), piso=piso, spam=spam, contacto_falso=falso)
