"""Normalización y validación de campos del lead. Funciones puras, sin acceso a la base."""
import re
import unicodedata

import phonenumbers

REGION_POR_DEFECTO = "EC"
EMAIL_MAX = 254

SERVICIOS = (
    "Blanqueamiento", "Combo limpieza + blanqueamiento", "Ortodoncia",
    "Alineadores", "Limpieza", "Valoración", "Otro",
)
PLATAFORMAS = ("instagram", "facebook")
CAMPANAS = ("Sonrisa Blanca", "Ortodoncia sin Excusas", "Chequeo Anual")

_SEPARADORES_TELEFONO = re.compile(r"[\s\-()]")
_TELEFONO_LIMPIO = re.compile(r"\+?[0-9]+")  # [0-9], no \d: \d admite dígitos de otros alfabetos
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def texto_seguro(s: str) -> str:
    # Escapa sustitutos UTF-16 sueltos, que no se pueden guardar en SQLite como UTF-8.
    return s.encode("utf-8", "backslashreplace").decode("utf-8")


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
    if not isinstance(valor, str):
        return None
    texto = valor.strip().lower()
    if len(texto) > EMAIL_MAX or not _EMAIL.fullmatch(texto):
        return None
    try:
        texto.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return texto


def normalizar_nombre(valor) -> str:
    return texto_seguro(" ".join(valor.split())) if isinstance(valor, str) else ""


def clave_nombre(valor) -> str:
    """Minúsculas, sin tildes y con espacios colapsados: compara nombres, no los muestra."""
    descompuesto = unicodedata.normalize("NFKD", normalizar_nombre(valor))
    sin_tildes = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return " ".join(sin_tildes.lower().split())


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
