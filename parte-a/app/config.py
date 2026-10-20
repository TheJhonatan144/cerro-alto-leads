"""Configuración leída de variables de entorno (no se carga .env automáticamente)."""
import os
from pathlib import Path

# Carpeta parte-a/: las rutas relativas se resuelven contra ella, no contra el cwd.
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_POR_DEFECTO = "data/leads.db"


def db_path() -> Path:
    valor = os.environ.get("DB_PATH", "").strip() or DB_PATH_POR_DEFECTO
    ruta = Path(valor)
    if not ruta.is_absolute():
        ruta = BASE_DIR / ruta
    return ruta
