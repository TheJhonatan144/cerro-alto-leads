"""Aislamiento común: los tests no leen el .env real ni dependen del entorno de la shell."""
import pytest

from app import config

VARIABLES = (
    "DB_PATH", "DUPLICADO_VENTANA_DIAS", "WORKER_ACTIVO", "WORKER_INTERVALO_S",
    "TRABAJO_LEASE_S", "TRABAJO_MAX_INTENTOS",
)


@pytest.fixture(autouse=True)
def entorno_aislado(monkeypatch):
    monkeypatch.setattr(config, "rutas_dotenv", lambda: [])
    for nombre in VARIABLES:
        monkeypatch.delenv(nombre, raising=False)
    # Sin hilo trabajador salvo en los tests que lo activan explícitamente.
    monkeypatch.setenv("WORKER_ACTIVO", "0")
