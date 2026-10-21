"""Cita tentativa según las reglas de la ficha de la clínica. Función pura.

No consulta ninguna agenda real: propone el primer hueco que cumple las reglas y lo marca
como tentativo, pendiente de confirmar disponibilidad. "ahora" y los feriados se reciben por
parámetro. Zona fija UTC-5 (Ecuador no tiene horario de verano).
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app import normalizar as n

ZONA = timezone(timedelta(hours=-5), "UTC-5")
FUENTE = "Ficha de la clínica"
NOTA_TENTATIVA = "tentativa, pendiente de confirmar disponibilidad real"
BLOQUE_MIN = 30
ANTICIPACION_MIN = timedelta(hours=2)
ANTICIPACION_MAX = timedelta(days=30)
MAX_PROPUESTAS = 2

# Horarios de atención (Ficha). Domingos y feriados, cerrado.
HORARIOS = {
    "norte": {"semana": (time(8, 0), time(19, 0)), "sabado": (time(8, 0), time(13, 0))},
    "valle": {"semana": (time(10, 0), time(19, 0)), "sabado": (time(9, 0), time(14, 0))},
}
SEDES = tuple(HORARIOS)


@dataclass(frozen=True)
class TipoCita:
    clave: str
    nombre: str
    duracion_min: int
    sedes: tuple[str, ...]
    precio_usd: int  # Ficha; nunca se inventan precios


# Tabla de citas y precios (fuente: Ficha de la clínica).
CITAS = {
    "limpieza_dental": TipoCita("limpieza_dental", "Limpieza dental", 60, ("norte", "valle"), 45),
    "valoracion_inicial": TipoCita("valoracion_inicial", "Valoración inicial", 30, ("norte", "valle"), 25),
    "valoracion_ortodoncia": TipoCita("valoracion_ortodoncia", "Valoración de ortodoncia", 30, ("norte", "valle"), 25),
}
NOTA_VALORACION_INICIAL = "USD 25, se descuenta del tratamiento si empieza dentro de 30 días"
# Promoción P2 (Ficha): valoración de ortodoncia sin costo de lunes a viernes.
P2_VIGENTE_HASTA = date(2027, 3, 31)
NOTA_P2 = "sin costo de lunes a viernes por la promoción P2 (vigente hasta 2027-03-31)"
NOTA_BLANQUEAMIENTO = ("el blanqueamiento exige una valoración en los últimos 3 meses y una limpieza en "
                       "los últimos 6, que no se pueden verificar: se propone primero la valoración inicial")
NOTA_ALINEADORES = "los alineadores solo se hacen en la sede Valle"

# Servicio del formulario (clave normalizada) -> (cita a proponer, sede exigida, nota). D12.
SERVICIO_A_CITA = {
    "limpieza": ("limpieza_dental", None, None),
    "valoracion": ("valoracion_inicial", None, None),
    "otro": ("valoracion_inicial", None, None),
    "blanqueamiento": ("valoracion_inicial", None, NOTA_BLANQUEAMIENTO),
    "combo limpieza + blanqueamiento": ("valoracion_inicial", None, NOTA_BLANQUEAMIENTO),
    "ortodoncia": ("valoracion_ortodoncia", None, None),
    "alineadores": ("valoracion_ortodoncia", "valle", NOTA_ALINEADORES),
}
POR_DEFECTO = ("valoracion_inicial", None, None)  # servicio ausente o desconocido


@dataclass(frozen=True)
class Propuesta:
    orden: int
    clave: str
    servicio_propuesto: str
    servicio_interes: str | None
    sede: str
    inicio: datetime  # con zona UTC-5
    fin: datetime
    duracion_min: int
    precio_usd: int
    nota: str


def _redondear_a_bloque(momento: datetime) -> datetime:
    """Sube al siguiente :00 o :30 (si no está ya exactamente en uno)."""
    base = momento.replace(second=0, microsecond=0)
    if base != momento:
        base += timedelta(minutes=1)
    resto = base.minute % BLOQUE_MIN
    return base + timedelta(minutes=(BLOQUE_MIN - resto) % BLOQUE_MIN)


def _horario(dia: date, sede: str, feriados) -> tuple[datetime, datetime] | None:
    if dia.weekday() == 6 or dia in feriados:
        return None
    apertura, cierre = HORARIOS[sede]["semana" if dia.weekday() < 5 else "sabado"]
    return (datetime.combine(dia, apertura, tzinfo=ZONA), datetime.combine(dia, cierre, tzinfo=ZONA))


def _primer_hueco(dia: date, sede: str, duracion: timedelta, desde: datetime, hasta: datetime, feriados):
    horario = _horario(dia, sede, feriados)
    if horario is None:
        return None
    apertura, cierre = horario
    inicio = _redondear_a_bloque(max(apertura, desde))
    if inicio > hasta or inicio + duracion > cierre:  # debe TERMINAR a más tardar al cierre
        return None
    return inicio


def _precio_y_nota(tipo: TipoCita, inicio: datetime) -> tuple[int, str | None]:
    if tipo.clave == "valoracion_ortodoncia":
        if inicio.weekday() < 5 and inicio.date() <= P2_VIGENTE_HASTA:
            return 0, NOTA_P2
        return tipo.precio_usd, None
    if tipo.clave == "valoracion_inicial":
        return tipo.precio_usd, NOTA_VALORACION_INICIAL
    return tipo.precio_usd, None


def proponer_citas(servicio_formulario, sede_habitual, ahora: datetime, feriados=()) -> list[Propuesta]:
    """Hasta 2 propuestas: el hueco más próximo y la siguiente alternativa en otro día."""
    if ahora.tzinfo is None:
        ahora = ahora.replace(tzinfo=timezone.utc)
    ahora = ahora.astimezone(ZONA)
    feriados = set(feriados)
    clave_servicio = n.clave_nombre(servicio_formulario) if isinstance(servicio_formulario, str) else ""
    clave_cita, sede_exigida, nota_servicio = SERVICIO_A_CITA.get(clave_servicio, POR_DEFECTO)
    tipo = CITAS[clave_cita]
    # La sede que exige el servicio manda (alineadores solo en Valle); si no, la habitual del
    # paciente; si no, Norte.
    sede = sede_exigida or (sede_habitual if sede_habitual in tipo.sedes else None) or "norte"
    duracion = timedelta(minutes=tipo.duracion_min)
    desde = _redondear_a_bloque(ahora + ANTICIPACION_MIN)
    hasta = ahora + ANTICIPACION_MAX

    propuestas, dia = [], desde.date()
    while dia <= hasta.date() and len(propuestas) < MAX_PROPUESTAS:
        inicio = _primer_hueco(dia, sede, duracion, desde, hasta, feriados)
        if inicio is not None:
            precio, nota_precio = _precio_y_nota(tipo, inicio)
            nota = "; ".join(x for x in (NOTA_TENTATIVA, nota_precio, nota_servicio) if x)
            propuestas.append(Propuesta(
                orden=len(propuestas) + 1, clave=tipo.clave, servicio_propuesto=tipo.nombre,
                servicio_interes=servicio_formulario if isinstance(servicio_formulario, str) else None,
                sede=sede, inicio=inicio, fin=inicio + duracion, duracion_min=tipo.duracion_min,
                precio_usd=precio, nota=nota))
        dia += timedelta(days=1)
    return propuestas
