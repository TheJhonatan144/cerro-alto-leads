"""Enmascarado del texto antes de enviarlo al LLM. Función pura.

Al LLM solo se envían el mensaje enmascarado, el servicio y la campaña (contenido_para_llm).
Nunca el nombre, teléfono, correo, ciudad, datos del paciente ni el enriquecimiento.
"""
import re
import unicodedata

from app import normalizar as n

MAX_CARACTERES = 1000
MAX_CAMPO_CORTO = 100
MIN_DIGITOS_NUMERO = 7
MARCA_CORTE = " […] "

_CORREO = re.compile(r"[^\s@<>()\[\],;:\"']+@[^\s@<>()\[\],;:\"']+\.[a-zA-Z]{2,}")
_ENLACE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:com|net|org|ec|io|ly|me|co|info|biz|xyz|example)(?:/\S*)?",
    re.IGNORECASE,
)
# Documento alfanumérico (pasaporte y similares): 1-2 letras + 6-9 dígitos.
_DOCUMENTO = re.compile(r"\b[A-Za-z]{1,2}\d{6,9}\b")
# Dígitos unidos por separadores (espacios, guiones, puntos, paréntesis, +). Si suman 7 o
# más dígitos es un número personal (teléfono, cédula, RUC…): [NUMERO]. Los dos puntos no
# separan, así que las horas ("17:00") quedan fuera.
_SECUENCIA = re.compile(r"[+(]?\d(?:[\s().+-]*\d)*\)?")
_PALABRA = re.compile(r"\w+")
_ESPACIOS = re.compile(r"[ \t]+")
_FRASE = re.compile(r"(?<=[.!?…])\s+")


def _sin_control(texto: str) -> str:
    # Caracteres de control y de formato (incluye saltos de línea y marcas invisibles).
    return "".join(" " if unicodedata.category(c) in ("Cc", "Cf") else c for c in texto)


def _numero(m: re.Match) -> str:
    digitos = sum(c.isdigit() for c in m.group(0))
    return "[NUMERO]" if digitos >= MIN_DIGITOS_NUMERO else m.group(0)


def _colapsar_repetidos(texto: str) -> str:
    """Quita frases repetidas (se conserva la primera aparición)."""
    vistas, frases = set(), []
    for frase in _FRASE.split(texto):
        clave = " ".join(_PALABRA.findall(n.clave_nombre(frase)))
        if clave and clave in vistas:
            continue
        vistas.add(clave)
        frases.append(frase)
    return " ".join(frases)


def _recortar(texto: str, max_len: int) -> str:
    """Si excede, conserva el inicio y el final (la intención suele estar al final)."""
    if len(texto) <= max_len:
        return texto
    mitad = max_len // 2
    return texto[:mitad].rstrip() + MARCA_CORTE + texto[-mitad:].lstrip()


def enmascarar(texto, nombre=None, max_len: int = MAX_CARACTERES) -> str:
    if not isinstance(texto, str):
        return ""
    t = _sin_control(texto)
    t = _CORREO.sub("[CORREO]", t)
    t = _ENLACE.sub("[ENLACE]", t)
    t = _DOCUMENTO.sub("[DOCUMENTO]", t)
    t = _SECUENCIA.sub(_numero, t)
    palabras_nombre = set(n.palabras_nombre(nombre)) if nombre else set()  # sin partículas
    palabras_nombre = {p for p in palabras_nombre if len(p) >= 2}
    if palabras_nombre:
        t = _PALABRA.sub(lambda m: "[NOMBRE]" if n.clave_nombre(m.group(0)) in palabras_nombre
                         else m.group(0), t)
    t = _ESPACIOS.sub(" ", t).strip()
    return _recortar(_colapsar_repetidos(t), max_len)


def contenido_para_llm(mensaje, *, servicio=None, campana=None, nombre=None) -> dict:
    """Lo único que sale hacia el LLM."""
    return {
        "mensaje": enmascarar(mensaje, nombre),
        "servicio": enmascarar(servicio, nombre, MAX_CAMPO_CORTO),
        "campana": enmascarar(campana, nombre, MAX_CAMPO_CORTO),
    }
