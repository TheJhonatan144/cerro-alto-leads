"""Paso 6: los ejemplos de parte-a/n8n/ejemplos coinciden con lo que genera el código."""
import json
from pathlib import Path

import pytest

from app import ejemplos_n8n
from app.canales import cuerpo_n8n_aviso, cuerpo_n8n_error

DIR = Path(__file__).resolve().parents[1] / "n8n" / "ejemplos"


@pytest.mark.parametrize("nombre", ["aviso_caliente", "novedad_duplicado", "error", "resumen"])
def test_el_ejemplo_coincide_con_el_codigo(nombre):
    generado = ejemplos_n8n.generar()[nombre]
    en_disco = json.loads((DIR / f"{nombre}.json").read_text(encoding="utf-8"))
    assert en_disco == generado, f"regenera con: python -m app.ejemplos_n8n"


def test_los_ejemplos_cumplen_el_contrato():
    e = ejemplos_n8n.generar()
    for nombre in ("aviso_caliente", "novedad_duplicado"):
        assert set(e[nombre]) == set(cuerpo_n8n_aviso({"tipo": "", "evento_id": 0, "creado_at": ""}, ""))
    assert set(e["error"]) == set(cuerpo_n8n_error({"codigo": ""}))
    assert e["aviso_caliente"]["tipo"] == "lead_caliente" and e["novedad_duplicado"]["tipo"] == "novedad_duplicado"
    assert set(e["resumen"]) == {"generado_at", "desde", "salud", "eventos", "rutas", "pendiente_mas_antiguo_s",
                                 "avisos", "seguimientos_pendientes", "incidencias_abiertas", "hilos", "alertas"}


def test_los_ejemplos_son_ficticios():
    texto = json.dumps(ejemplos_n8n.generar(), ensure_ascii=False)
    assert "example.com" in texto or "Ejemplo" in texto
    for real in ("Carla", "Mendoza", "0991234567", "Daniela"):
        assert real not in texto
