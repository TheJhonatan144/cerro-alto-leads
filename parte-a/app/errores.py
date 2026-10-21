"""Señales compartidas entre etapas y trabajador, y el punto único del canal de errores."""
import logging

log = logging.getLogger("leads.errores")

# Reprogramaciones por trabajo (cupo local agotado o 429). Después, la etapa degrada (D5).
MAX_REPROGRAMACIONES = 3
# Tope de espera de una reprogramación, también para Retry-After.
REPROGRAMAR_MAX_S = 120


class Reprogramar(Exception):
    """La etapa no puede seguir ahora pero sí más tarde: el trabajador la reprograma sin
    consumir intento y sin esperar dentro de la etapa."""

    def __init__(self, segundos: float):
        super().__init__(f"reprogramar en {segundos} s")
        self.segundos = max(1.0, min(float(segundos), REPROGRAMAR_MAX_S))


def reportar_error(origen: str, codigo: str, *, evento_id=None, nivel: int = logging.WARNING) -> None:
    """Único punto de llamada del canal de errores. Hoy solo registra; el Bloque 5 lo enlaza
    con el canal de avisos. Nunca recibe datos personales: solo códigos cortos e ids internos."""
    log.log(nivel, "error de integración origen=%s codigo=%s evento=%s", origen, codigo, evento_id)
