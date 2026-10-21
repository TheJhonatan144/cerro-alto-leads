"""Normalización y validación de campos del lead. Funciones puras, sin acceso a la base."""
import re
import unicodedata
from collections import Counter

import phonenumbers

REGION_POR_DEFECTO = "EC"
# Tope de longitud antes de cualquier expresión regular (reglas, enmascarado, colapso).
# El webhook no tiene autenticación ni tope de cuerpo y hay un solo trabajador: un mensaje
# enorme no puede bloquearlo. Se conservan el inicio y el final.
MAX_TEXTO_PROCESADO = 20_000
MARCA_CORTE = " […] "
EMAIL_MAX = 254

SERVICIOS = (
    "Blanqueamiento", "Combo limpieza + blanqueamiento", "Ortodoncia",
    "Alineadores", "Limpieza", "Valoración", "Otro",
)
PLATAFORMAS = ("instagram", "facebook")
CAMPANAS = ("Sonrisa Blanca", "Ortodoncia sin Excusas", "Chequeo Anual")

_SEPARADORES_TELEFONO = re.compile(r"[\s\-().]")
_TELEFONO_LIMPIO = re.compile(r"\+?[0-9]+")  # [0-9], no \d: \d admite dígitos de otros alfabetos
_EMAIL_LOCAL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}")
_EMAIL_ETIQUETA = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")  # 1-63, sin '-' en los bordes
_EMAIL_TLD = re.compile(r"[a-z]{2,}|xn--[a-z0-9-]+")
EMAIL_DOMINIO_MAX = 253


def acotar_texto(texto: str, maximo: int = MAX_TEXTO_PROCESADO) -> str:
    """Si excede 'maximo', conserva la primera y la última mitad con una marca de corte."""
    if len(texto) <= maximo:
        return texto
    mitad = maximo // 2
    return texto[:mitad] + MARCA_CORTE + texto[-mitad:]


def texto_seguro(s: str) -> str:
    # Escapa sustitutos UTF-16 sueltos, que no se pueden guardar en SQLite como UTF-8.
    return s.encode("utf-8", "backslashreplace").decode("utf-8")


def normalizar_event_id(valor) -> str | None:
    """Texto recortado o entero (no booleano) como texto; cualquier otro tipo o vacío -> None.

    La usan el webhook y, si allí falló, el trabajador al volver a extraerlo.
    """
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return str(valor)
    if isinstance(valor, str):
        return texto_seguro(valor.strip()) or None
    return None


def normalizar_telefono(valor) -> str | None:
    """E.164 o None. Región por defecto EC; 12 dígitos que empiezan con 593 = prefijo sin '+'."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        texto = str(valor)
    elif isinstance(valor, str):
        texto = _SEPARADORES_TELEFONO.sub("", valor)
    else:
        return None
    if not _TELEFONO_LIMPIO.fullmatch(texto):
        return None
    if not texto.startswith("+") and len(texto) == 12 and texto.startswith("593"):
        texto = "+" + texto
    try:
        numero = phonenumbers.parse(texto, None if texto.startswith("+") else REGION_POR_DEFECTO)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(numero):
        return None
    return phonenumbers.format_number(numero, phonenumbers.PhoneNumberFormat.E164)


def normalizar_email(valor) -> str | None:
    """Correo ASCII estricto, recortado y en minúsculas, o None. Sin consultar dominios."""
    if not isinstance(valor, str):
        return None
    recortado = valor.strip()
    # ASCII se comprueba antes de .lower(): "K" (signo Kelvin) pasaría a "k" ASCII.
    if not recortado.isascii():
        return None
    texto = recortado.lower()
    if len(texto) > EMAIL_MAX or texto.count("@") != 1:
        return None
    local, dominio = texto.split("@")
    # Las regex solo admiten caracteres imprimibles: excluyen controles y espacios.
    if not _EMAIL_LOCAL.fullmatch(local) or local[0] == "." or local[-1] == "." or ".." in local:
        return None
    if len(dominio) > EMAIL_DOMINIO_MAX:
        return None
    etiquetas = dominio.split(".")
    if len(etiquetas) < 2 or not all(_EMAIL_ETIQUETA.fullmatch(e) for e in etiquetas):
        return None
    if not _EMAIL_TLD.fullmatch(etiquetas[-1]):
        return None
    return texto


def normalizar_nombre(valor) -> str:
    return texto_seguro(" ".join(valor.split())) if isinstance(valor, str) else ""


def clave_nombre(valor) -> str:
    """Minúsculas, sin tildes y con espacios colapsados: compara nombres, no los muestra."""
    descompuesto = unicodedata.normalize("NFKD", normalizar_nombre(valor))
    sin_tildes = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return " ".join(sin_tildes.lower().split())


PARTICULAS_NOMBRE = frozenset({"de", "del", "la", "las", "los", "y"})
MIN_PALABRAS_NOMBRE = 2  # un nombre de una sola palabra nunca identifica a una persona


def palabras_nombre(valor) -> tuple[str, ...]:
    """Palabras del nombre sin tildes ni mayúsculas, sin partículas (de, del, la, ...)."""
    return tuple(p for p in clave_nombre(valor).split() if p not in PARTICULAS_NOMBRE)


def _contenido(corto: Counter, largo: Counter) -> bool:
    return all(largo[p] >= k for p, k in corto.items())


def elegir_por_nombre(nombre_lead, nombres_candidatos) -> int | None:
    """D7.2: índice de la única candidata compatible con el lead, o None (ambigua).

    Una candidata es compatible si tiene las mismas palabras (en cualquier orden) o si todas
    las palabras del nombre más corto están en el más largo y el más corto tiene al menos
    2 palabras (D7.1). La coincidencia exacta NO tiene prioridad (D7.2): si hay más de una
    compatible, es ambigua. Un lead vacío o de una sola palabra nunca confirma.
    Las palabras se cuentan ("Carla Carla" no está contenido en "Carla Mendoza").
    """
    lead = Counter(palabras_nombre(nombre_lead))
    if sum(lead.values()) < MIN_PALABRAS_NOMBRE:
        return None
    compatibles = []
    for i, nombre in enumerate(nombres_candidatos):
        c = Counter(palabras_nombre(nombre))
        # La igualdad exacta es el caso en que cada uno contiene al otro.
        corto, largo = (c, lead) if sum(c.values()) <= sum(lead.values()) else (lead, c)
        if sum(corto.values()) >= MIN_PALABRAS_NOMBRE and _contenido(corto, largo):
            compatibles.append(i)
    return compatibles[0] if len(compatibles) == 1 else None


def normalizar_texto(valor) -> str | None:
    """str recortado; lista de str unida con ", "; otro tipo o vacío -> None."""
    if isinstance(valor, str):
        texto = valor.strip()
    elif isinstance(valor, list) and all(isinstance(x, str) for x in valor):
        texto = ", ".join(x.strip() for x in valor if x.strip())
    else:
        return None
    return texto_seguro(texto) if texto else None


def normalizar_catalogo(valor, conocidos, adv_ausente: str, adv_desconocido: str):
    """(valor canónico o texto tal cual, advertencia o None). Un valor desconocido no rechaza."""
    texto = normalizar_texto(valor)
    if texto is None:
        return None, adv_ausente
    clave = clave_nombre(texto)
    for conocido in conocidos:
        if clave_nombre(conocido) == clave:
            return conocido, None
    return texto, adv_desconocido
