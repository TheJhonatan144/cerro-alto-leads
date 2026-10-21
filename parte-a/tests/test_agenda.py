"""Bloque 5a: cita tentativa con las reglas de la ficha (agenda.py, función pura)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.agenda import ZONA, proponer_citas

UTC = timezone.utc


def ec(y, m, d, h, mi=0):
    """Hora de Ecuador (UTC-5)."""
    return datetime(y, m, d, h, mi, tzinfo=ZONA)


def primera(servicio, ahora, sede=None, feriados=()):
    p = proponer_citas(servicio, sede, ahora, feriados)
    assert p, "sin propuestas"
    return p[0]


# --- Horarios, anticipación y bloques ---------------------------------------------------------------

@pytest.mark.parametrize("servicio, sede, ahora, inicio_esperado, sede_esperada", [
    # Lunes 08:00 en Norte: anticipación mínima 2 h -> 10:00.
    ("Valoración", None, ec(2026, 11, 2, 8, 0), ec(2026, 11, 2, 10, 0), "norte"),
    # Bloques de 30 min: 08:07 + 2 h = 10:07 -> 10:30.
    ("Valoración", None, ec(2026, 11, 2, 8, 7), ec(2026, 11, 2, 10, 30), "norte"),
    # Limpieza (60 min) en Valle un sábado: 13:30 terminaría 14:30 (> 14:00) -> lunes 10:00.
    ("Limpieza", "valle", ec(2026, 11, 7, 11, 20), ec(2026, 11, 9, 10, 0), "valle"),
    # Limpieza en Valle un sábado a las 11:00: 13:00 termina justo a las 14:00 -> vale.
    ("Limpieza", "valle", ec(2026, 11, 7, 11, 0), ec(2026, 11, 7, 13, 0), "valle"),
    # Norte entre semana: 18:30 + 60 min = 19:30 > 19:00 -> martes 08:00.
    ("Limpieza", None, ec(2026, 11, 2, 16, 10), ec(2026, 11, 3, 8, 0), "norte"),
    # Sábado 13:00 en Norte (cierra a las 13:00) -> el domingo cierra -> lunes 08:00.
    ("Valoración", None, ec(2026, 11, 7, 13, 0), ec(2026, 11, 9, 8, 0), "norte"),
    # Valle abre a las 10:00 entre semana.
    ("Valoración", "valle", ec(2026, 11, 2, 6, 0), ec(2026, 11, 2, 10, 0), "valle"),
    # Valle sábado 09:00-14:00.
    ("Valoración", "valle", ec(2026, 11, 7, 6, 0), ec(2026, 11, 7, 9, 0), "valle"),
])
def test_horarios(servicio, sede, ahora, inicio_esperado, sede_esperada):
    p = primera(servicio, ahora, sede)
    assert p.inicio == inicio_esperado and p.sede == sede_esperada


def test_domingo_y_feriado_cerrados():
    ahora = ec(2026, 11, 8, 9, 0)  # domingo
    p = primera("Valoración", ahora, feriados=(date(2026, 11, 9),))  # lunes feriado
    assert p.inicio == ec(2026, 11, 10, 8, 0)


def test_anticipacion_maxima_30_dias():
    ahora = ec(2026, 11, 2, 8, 0)
    feriados = tuple(date(2026, 11, 2) + timedelta(days=k) for k in range(40))
    assert proponer_citas("Valoración", None, ahora, feriados) == []
    for p in proponer_citas("Valoración", None, ahora, ()):
        assert p.inicio <= ahora + timedelta(days=30)


def test_todas_las_propuestas_cumplen_las_reglas():
    ahora = ec(2026, 11, 2, 17, 45)
    for servicio in ("Limpieza", "Valoración", "Ortodoncia", "Alineadores", "Blanqueamiento", "Otro"):
        for p in proponer_citas(servicio, None, ahora, ()):
            assert p.inicio.minute in (0, 30) and p.inicio.second == 0
            assert p.inicio - ahora >= timedelta(hours=2)
            assert p.inicio.weekday() != 6
            assert p.fin - p.inicio == timedelta(minutes=p.duracion_min)
            cierre = {("norte", True): 19, ("norte", False): 13, ("valle", True): 19, ("valle", False): 14}
            limite = p.inicio.replace(hour=cierre[(p.sede, p.inicio.weekday() < 5)], minute=0)
            assert p.fin <= limite
            assert "tentativa, pendiente de confirmar disponibilidad real" in p.nota


def test_dos_propuestas_en_dias_distintos():
    p = proponer_citas("Valoración", None, ec(2026, 11, 2, 8, 0), ())
    assert len(p) == 2 and p[0].inicio.date() != p[1].inicio.date() and p[0].inicio < p[1].inicio


# --- Servicio del formulario -> cita (D12) -------------------------------------------------------------

@pytest.mark.parametrize("servicio, clave, duracion, precio", [
    ("Limpieza", "limpieza_dental", 60, 45),
    ("Valoración", "valoracion_inicial", 30, 25),
    ("Otro", "valoracion_inicial", 30, 25),
    (None, "valoracion_inicial", 30, 25),
    ("Servicio desconocido", "valoracion_inicial", 30, 25),
    ("Blanqueamiento", "valoracion_inicial", 30, 25),
    ("Combo limpieza + blanqueamiento", "valoracion_inicial", 30, 25),
])
def test_servicio_a_cita(servicio, clave, duracion, precio):
    p = primera(servicio, ec(2026, 11, 2, 8, 0))
    assert (p.clave, p.duracion_min, p.precio_usd) == (clave, duracion, precio)


def test_blanqueamiento_menciona_el_servicio_de_interes():
    p = primera("Blanqueamiento", ec(2026, 11, 2, 8, 0))
    assert "blanqueamiento" in p.nota.lower() and "valoración" in p.nota.lower()


def test_alineadores_siempre_en_valle():
    for sede in (None, "norte", "valle"):
        p = primera("Alineadores", ec(2026, 11, 2, 8, 0), sede)
        assert p.sede == "valle" and p.clave == "valoracion_ortodoncia"


def test_sede_habitual_del_paciente():
    assert primera("Limpieza", ec(2026, 11, 2, 8, 0), "valle").sede == "valle"
    assert primera("Limpieza", ec(2026, 11, 2, 8, 0), "norte").sede == "norte"
    assert primera("Limpieza", ec(2026, 11, 2, 8, 0), None).sede == "norte"
    assert primera("Limpieza", ec(2026, 11, 2, 8, 0), "sur").sede == "norte"  # valor desconocido


def test_p2_valoracion_de_ortodoncia_lunes_a_viernes_frente_a_sabado():
    lunes = primera("Ortodoncia", ec(2026, 11, 2, 8, 0))
    assert lunes.inicio.weekday() < 5 and lunes.precio_usd == 0 and "P2" in lunes.nota
    sabado = primera("Ortodoncia", ec(2026, 11, 7, 6, 0))
    assert sabado.inicio.weekday() == 5 and sabado.precio_usd == 25
    despues = primera("Ortodoncia", ec(2027, 4, 5, 6, 0))  # P2 vigente hasta 2027-03-31
    assert despues.inicio.weekday() < 5 and despues.precio_usd == 25 and "P2" not in despues.nota


def test_sin_horario_de_verano_y_ahora_en_utc():
    # Julio y noviembre: el mismo desfase fijo UTC-5.
    for ahora_utc in (datetime(2027, 7, 5, 13, 0, tzinfo=UTC), datetime(2026, 11, 2, 13, 0, tzinfo=UTC)):
        p = primera("Valoración", ahora_utc)
        assert p.inicio.utcoffset() == timedelta(hours=-5)
        assert p.inicio.hour == 10  # 13:00Z = 08:00 en Ecuador + 2 h
