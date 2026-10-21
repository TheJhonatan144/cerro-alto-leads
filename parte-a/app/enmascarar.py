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
MARCA_CORTE = n.MARCA_CORTE
MAX_ESPACIOS_CORREO = 1  # espacios tolerados junto a "@" y a cada punto del dominio
_ENLACE = re.compile(
    r"(?:https?://|www\.)\S+"
    # Sin protocolo: solo empieza al inicio de un tramo de letras, puntos y guiones (no en
    # cada etiqueta), así "a.a.a.…" se recorre una vez y no en tiempo cuadrático.
    r"|(?<![A-Za-z0-9.-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:com|net|org|ec|io|ly|me|co|info|biz|xyz|example)(?:/\S*)?",
    re.IGNORECASE,
)
# Documento alfanumérico (pasaporte y similares): 1-3 letras y 6-9 dígitos, en cualquier
# orden, juntos o con guion. Con espacio solo si las letras van en mayúsculas ("AB 123456"),
# para no tragarse palabras como "de", "al" o "es" antes de un número.
_DOCUMENTO = re.compile(
    r"\b(?:[A-Za-z]{1,3}-?|[A-Z]{1,3} )\d{6,9}\b"
    r"|\b\d{6,9}(?:-?[A-Za-z]{1,3}| [A-Z]{1,3})\b"
)
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


def _es_local(c: str) -> bool:
    return c.isalnum() or c in "._%+-"


def _es_dominio(c: str) -> bool:
    return c.isalnum() or c == "-"


def _saltar_espacios(t: str, i: int, paso: int, limite: int) -> int:
    """Avanza (paso=1) o retrocede (paso=-1) como mucho MAX_ESPACIOS_CORREO espacios."""
    for _ in range(MAX_ESPACIOS_CORREO):
        j = i if paso == 1 else i - 1
        if (j < limite if paso == 1 else j >= limite) and t[j] == " ":
            i += paso
        else:
            break
    return i


def _fin_dominio(t: str, i: int) -> int | None:
    """Final del dominio que empieza en i (con espacios tolerados junto a los puntos), o None."""
    n_ = len(t)
    etiquetas, fin = [], None
    while True:
        inicio = i
        while i < n_ and _es_dominio(t[i]):
            i += 1
        if i == inicio:
            break
        etiquetas.append(t[inicio:i])
        fin = i
        j = _saltar_espacios(t, i, 1, n_)
        if j < n_ and t[j] == ".":
            k = _saltar_espacios(t, j + 1, 1, n_)
            if k < n_ and _es_dominio(t[k]):
                i = k
                continue
        break
    tld = etiquetas[-1] if etiquetas else ""
    if len(etiquetas) >= 2 and len(tld) >= 2 and tld.isalpha():
        return fin
    return None


def _enmascarar_correos(t: str) -> str:
    """Tiempo lineal: parte de cada "@" y se expande a los lados, sin escanear la cadena
    completa por cada candidato. Tolera un espacio junto a "@" y a los puntos."""
    if "@" not in t:
        return t
    partes, ultimo = [], 0
    i = t.find("@")
    while i != -1:
        izq = _saltar_espacios(t, i, -1, ultimo)
        inicio = izq
        while inicio > ultimo and _es_local(t[inicio - 1]):
            inicio -= 1
        fin = None
        if inicio < izq:
            fin = _fin_dominio(t, _saltar_espacios(t, i + 1, 1, len(t)))
        if fin is not None:
            partes += [t[ultimo:inicio], "[CORREO]"]
            ultimo = fin
        i = t.find("@", max(i + 1, ultimo))
    partes.append(t[ultimo:])
    return "".join(partes)


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
    # NFC: "Mari\u0301a" y "María" son la misma palabra para \w y para el nombre.
    t = unicodedata.normalize("NFC", n.acotar_texto(texto))
    t = _sin_control(t)
    t = _enmascarar_correos(t)
    t = _ENLACE.sub("[ENLACE]", t)
    t = _DOCUMENTO.sub("[DOCUMENTO]", t)
    t = _SECUENCIA.sub(_numero, t)
    nombre = unicodedata.normalize("NFC", nombre) if isinstance(nombre, str) else None
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
