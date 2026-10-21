"""Bloque 2b: configuración validada al arranque y lector de .env."""
import shutil
from pathlib import Path

import pytest

from app import config
from app.main import create_app

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

DEFECTOS = config.Config(
    db_path=config.BASE_DIR / "data/leads.db",
    duplicado_ventana_dias=7,
    worker_activo=True,
    worker_intervalo_s=1,
    trabajo_lease_s=60,
    trabajo_max_intentos=5,
)


def test_sin_variables_usa_defectos():
    assert config.cargar_config({}) == DEFECTOS


def test_valores_vacios_usan_defectos():
    entorno = {nombre: "  " for nombre in (
        "DB_PATH", "DUPLICADO_VENTANA_DIAS", "WORKER_ACTIVO", "WORKER_INTERVALO_S",
        "TRABAJO_LEASE_S", "TRABAJO_MAX_INTENTOS")}
    assert config.cargar_config(entorno) == DEFECTOS


def test_copia_literal_de_env_example_usa_defectos(tmp_path):
    copia = tmp_path / ".env"
    shutil.copyfile(ENV_EXAMPLE, copia)
    entorno = {}
    assert config.cargar_env([copia], entorno) == copia
    for nombre in ("WORKER_ACTIVO", "WORKER_INTERVALO_S", "TRABAJO_LEASE_S", "TRABAJO_MAX_INTENTOS",
                   "DB_PATH", "DUPLICADO_VENTANA_DIAS"):
        assert entorno[nombre] == "", nombre  # .env.example las declara vacías
    assert config.cargar_config(entorno) == DEFECTOS


def test_valores_validos():
    cfg = config.cargar_config({"WORKER_ACTIVO": "0", "WORKER_INTERVALO_S": "3",
                                "TRABAJO_LEASE_S": " 120 ", "TRABAJO_MAX_INTENTOS": "2",
                                "DUPLICADO_VENTANA_DIAS": "10"})
    assert (cfg.worker_activo, cfg.worker_intervalo_s, cfg.trabajo_lease_s,
            cfg.trabajo_max_intentos, cfg.duplicado_ventana_dias) == (False, 3, 120, 2, 10)


@pytest.mark.parametrize("nombre, valor", [
    ("WORKER_INTERVALO_S", "abc"),
    ("WORKER_INTERVALO_S", "0"),
    ("TRABAJO_LEASE_S", "-5"),
    ("TRABAJO_LEASE_S", "1.5"),
    ("TRABAJO_MAX_INTENTOS", "0"),
    ("TRABAJO_MAX_INTENTOS", "cinco"),
    ("DUPLICADO_VENTANA_DIAS", "siete"),
    ("DUPLICADO_VENTANA_DIAS", "0"),
    ("WORKER_ACTIVO", "si"),
    ("WORKER_ACTIVO", "2"),
])
def test_valor_invalido_hace_fallar_el_arranque(tmp_path, monkeypatch, nombre, valor):
    monkeypatch.setenv(nombre, valor)
    with pytest.raises(config.ConfigError, match=nombre):
        create_app(tmp_path / "leads.db")
    assert not (tmp_path / "leads.db").exists()  # falla antes de tocar la base


def test_lector_env_ignora_comentarios_y_no_pisa_el_entorno(tmp_path):
    archivo = tmp_path / ".env"
    archivo.write_text(
        "# comentario\n"
        "\n"
        "TRABAJO_LEASE_S=90\n"
        "  WORKER_INTERVALO_S = 2  \n"
        "export TRABAJO_MAX_INTENTOS=3\n"
        'DB_PATH="/tmp/x.db"\n'
        "LINEA_SIN_IGUAL\n"
        "WORKER_ACTIVO=1\n",
        encoding="utf-8",
    )
    entorno = {"WORKER_ACTIVO": "0"}
    config.cargar_env([archivo], entorno)
    assert entorno == {
        "WORKER_ACTIVO": "0",  # ya definida: no se pisa
        "TRABAJO_LEASE_S": "90",
        "WORKER_INTERVALO_S": "2",
        "TRABAJO_MAX_INTENTOS": "3",
        "DB_PATH": "/tmp/x.db",
    }


def test_lector_env_busca_primero_aqui_y_luego_arriba(tmp_path, monkeypatch):
    monkeypatch.undo()  # sin el aislamiento de conftest: usar rutas_dotenv real
    arriba, aqui = tmp_path, tmp_path / "parte-a"
    aqui.mkdir()
    (arriba / ".env").write_text("TRABAJO_LEASE_S=11\n", encoding="utf-8")
    monkeypatch.chdir(aqui)
    entorno = {}
    assert config.cargar_env(entorno=entorno) == arriba / ".env"
    assert entorno["TRABAJO_LEASE_S"] == "11"

    (aqui / ".env").write_text("TRABAJO_LEASE_S=22\n", encoding="utf-8")
    entorno = {}
    assert config.cargar_env(entorno=entorno) == aqui / ".env"
    assert entorno["TRABAJO_LEASE_S"] == "22"


def test_sin_env_no_falla(tmp_path):
    assert config.cargar_env([tmp_path / "no-existe"], {}) is None
