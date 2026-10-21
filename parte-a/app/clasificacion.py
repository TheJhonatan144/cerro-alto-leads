"""Validación estricta de la salida del LLM y decisión de la ruta. Funciones puras.

La salida del LLM es un dato no confiable: se valida campo a campo y su 'motivo' nunca se
usa para decidir. Las reglas deterministas son el piso: el LLM no puede rebajar un caliente.
"""
import json
import math
import re
from dataclasses import dataclass

from app.datos_reglas import ETIQUETAS
from app.reglas import SENALES_CALIENTES, ResultadoReglas

INTENCIONES = ("agendar", "informacion", "precio", "urgencia", "tratamiento_alto_valor", "spam", "otro")
URGENCIAS = ("alta", "media", "baja", "ninguna")
ACCIONES = ("llamar_ya", "agendar_cita", "enviar_informacion", "seguimiento", "descartar")
RUTAS = ("caliente", "tibio", "basura")
PRESUPUESTO_MAX_USD = 20000
MOTIVO_MAX = 200
MAX_CARACTERES_SALIDA = 8000
MAX_PROFUNDIDAD = 3  # el objeto raíz cuenta como 1
MAX_DIGITOS_ENTERO = 15  # un entero mayor no se convierte a float (OverflowError)
CAMPOS = ("intencion", "urgencia", "presupuesto_estimado_usd", "siguiente_accion",
          "ruta_sugerida", "confianza", "motivo")

_CERCA = re.compile(r"\A```(?:json)?[ \t]*\n?(.*?)\n?```\Z", re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class SalidaInvalida(ValueError):
    def __init__(self, codigo: str):
        super().__init__(codigo)
        self.codigo = codigo


@dataclass(frozen=True)
class SalidaLLM:
    intencion: str
    urgencia: str
    presupuesto_estimado_usd: float | None
    siguiente_accion: str
    ruta_sugerida: str
    confianza: float
    motivo: str  # NO confiable: solo informativo, nunca decide


def _sin_duplicados(pares):
    claves = [k for k, _ in pares]
    if len(claves) != len(set(claves)):
        raise SalidaInvalida("clave_duplicada")
    return dict(pares)


def _rechazar_constante(_valor):
    raise SalidaInvalida("numero_no_finito")  # NaN, Infinity


def _profundidad(x, nivel=1) -> int:
    if isinstance(x, dict):
        return max([nivel] + [_profundidad(v, nivel + 1) for v in x.values()])
    if isinstance(x, list):
        return max([nivel] + [_profundidad(v, nivel + 1) for v in x])
    return nivel - 1


def _numero(valor) -> bool:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return False
    if isinstance(valor, int):
        return abs(valor) < 10 ** MAX_DIGITOS_ENTERO
    return math.isfinite(valor)


def _enum(datos, campo, opciones) -> str:
    valor = datos[campo]
    if not isinstance(valor, str) or valor not in opciones:
        raise SalidaInvalida(f"{campo}_invalido")
    return valor


def validar_salida_llm(texto) -> SalidaLLM:
    """Valida la salida del LLM. Cualquier fallo, incluso uno inesperado, es SalidaInvalida."""
    try:
        return _validar(texto)
    except SalidaInvalida:
        raise
    except Exception:
        raise SalidaInvalida("error_de_validacion") from None


def _validar(texto) -> SalidaLLM:
    if not isinstance(texto, str) or len(texto) > MAX_CARACTERES_SALIDA:
        raise SalidaInvalida("tamano")
    t = texto.strip()
    cerca = _CERCA.match(t)
    if cerca:
        t = cerca.group(1).strip()
    if not (t.startswith("{") and t.endswith("}")):
        raise SalidaInvalida("texto_extra")
    try:
        datos = json.loads(t, object_pairs_hook=_sin_duplicados, parse_constant=_rechazar_constante)
    except SalidaInvalida:
        raise
    except (ValueError, RecursionError):  # JSON inválido o con texto extra detrás
        raise SalidaInvalida("json_invalido") from None
    if not isinstance(datos, dict):
        raise SalidaInvalida("no_es_objeto")
    if _profundidad(datos) > MAX_PROFUNDIDAD:
        raise SalidaInvalida("anidamiento")
    if any(c not in datos for c in CAMPOS):
        raise SalidaInvalida("faltan_campos")

    presupuesto = datos["presupuesto_estimado_usd"]
    if presupuesto is not None and not (_numero(presupuesto) and 0 <= presupuesto <= PRESUPUESTO_MAX_USD):
        raise SalidaInvalida("presupuesto_invalido")
    confianza = datos["confianza"]
    if not (_numero(confianza) and 0 <= confianza <= 1):
        raise SalidaInvalida("confianza_invalida")
    motivo = datos["motivo"]
    if not isinstance(motivo, str) or len(motivo) > MOTIVO_MAX:
        raise SalidaInvalida("motivo_invalido")
    salida = SalidaLLM(
        intencion=_enum(datos, "intencion", INTENCIONES),
        urgencia=_enum(datos, "urgencia", URGENCIAS),
        presupuesto_estimado_usd=None if presupuesto is None else float(presupuesto),
        siguiente_accion=_enum(datos, "siguiente_accion", ACCIONES),
        ruta_sugerida=_enum(datos, "ruta_sugerida", RUTAS),
        confianza=float(confianza),
        motivo=_CONTROL.sub(" ", motivo).strip(),
    )
    # Incoherencias: descartar va con basura y solo con basura; y basura exige intención spam
    # (u "otro" sin ninguna urgencia): "informacion" + basura no tiene sentido.
    if (salida.ruta_sugerida == "basura") != (salida.siguiente_accion == "descartar"):
        raise SalidaInvalida("incoherente")
    if salida.ruta_sugerida == "basura" and not (
            salida.intencion == "spam" or (salida.intencion == "otro" and salida.urgencia == "ninguna")):
        raise SalidaInvalida("incoherente")
    return salida


@dataclass(frozen=True)
class Decision:
    ruta: str
    motivo: str  # en español, generado aquí; nunca copia texto del lead ni del LLM
    siguiente_accion: str
    senales: tuple[str, ...]


def _etiquetas(senales) -> str:
    return ", ".join(ETIQUETAS[s] for s in senales if s in ETIQUETAS)


ACCIONES_CALIENTE = ("llamar_ya", "agendar_cita")


def _accion(ruta: str, reglas: ResultadoReglas, llm: SalidaLLM | None) -> str:
    """La del LLM si coincide con la ruta final (y, en caliente, es llamar o agendar);
    si no, se deriva de las reglas."""
    if llm is not None and llm.ruta_sugerida == ruta and (
            ruta != "caliente" or llm.siguiente_accion in ACCIONES_CALIENTE):
        return llm.siguiente_accion
    if ruta == "caliente":
        if "urgencia_clinica" in reglas.senales:
            return "llamar_ya"
        if "pide_agendar_o_llamada" in reglas.senales:
            return "agendar_cita"
        return "llamar_ya" if llm is not None and llm.urgencia == "alta" else "agendar_cita"
    return "descartar" if ruta == "basura" else "enviar_informacion"


def _decision(ruta, motivo, reglas, llm) -> Decision:
    return Decision(ruta, motivo, _accion(ruta, reglas, llm), reglas.senales)


def _llm_caliente_coherente(llm: SalidaLLM) -> bool:
    return (llm.ruta_sugerida == "caliente"
            and (llm.intencion in ("agendar", "urgencia", "tratamiento_alto_valor")
                 or llm.urgencia in ("alta", "media")))


def decidir_ruta(reglas: ResultadoReglas, llm: SalidaLLM | None, umbral_basura: float = 0.8) -> Decision:
    # (a) Piso de las reglas: el LLM no puede rebajarlo.
    if reglas.piso == "caliente":
        calientes = [s for s in reglas.senales if s in SENALES_CALIENTES]
        return _decision("caliente", f"Caliente por reglas: {_etiquetas(calientes)}", reglas, llm)
    # (c) Basura con evidencia de las reglas (spam o contactos falsos sin señales positivas).
    if (reglas.spam or reglas.contacto_falso) and not reglas.positivas:
        evidencia = [s for s in ("spam", "contacto_falso") if s in reglas.senales]
        return _decision("basura", f"Basura por reglas: {_etiquetas(evidencia)}", reglas, llm)
    if llm is not None:
        # Si el mensaje intenta manipular al sistema, la sugerencia del LLM no cuenta en
        # ninguna dirección: ni para subir a caliente ni para bajar a basura.
        if reglas.inyeccion and llm.ruta_sugerida != "tibio":
            return _decision("tibio", f"Tibio: el LLM sugirió {llm.ruta_sugerida}, pero el mensaje "
                             "contiene instrucciones dirigidas al sistema; se ignora", reglas, llm)
        # (b) Caliente del LLM solo si es coherente.
        if llm.ruta_sugerida == "caliente":
            if _llm_caliente_coherente(llm):
                return _decision("caliente", f"Caliente según el LLM: intención {llm.intencion}, "
                                 f"urgencia {llm.urgencia}", reglas, llm)
            return _decision("tibio", "Tibio: el LLM sugirió caliente sin intención ni urgencia "
                             "que lo justifiquen", reglas, llm)
        # (c) Basura del LLM solo con intención spam, confianza alta y sin señales positivas.
        # Un "otro" sin urgencia (p. ej. un saludo vacío) no es evidencia: queda tibio.
        if llm.ruta_sugerida == "basura":
            if llm.intencion == "spam" and llm.confianza >= umbral_basura and not reglas.positivas:
                return _decision("basura", f"Basura según el LLM (confianza {llm.confianza:.2f}) "
                                 "sin señales positivas en las reglas", reglas, llm)
            return _decision("tibio", "Tibio: el LLM sugirió basura sin evidencia suficiente "
                             "(ante la duda, tibio)", reglas, llm)
        return _decision("tibio", f"Tibio según el LLM: intención {llm.intencion}", reglas, llm)
    # (d) Sin LLM.
    if "interes_en_servicio" in reglas.senales:
        return _decision("tibio", "Tibio por reglas: interés real sin urgencia", reglas, llm)
    return _decision("tibio", "Tibio por defecto: sin señales suficientes (ante la duda, tibio)",
                     reglas, llm)
