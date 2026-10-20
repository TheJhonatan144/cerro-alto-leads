"""Configuración leída de variables de entorno (no se carga .env automáticamente)."""
import os
from pathlib import Path

# Carpeta parte-a/: las rutas relativas se resuelven contra ella, no contra el cwd.
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_POR_DEFECTO = "data/leads.db"
DUPLICADO_VENTANA_DIAS_POR_DEFECTO = 7


def db_path() -> Path:
    valor = os.environ.get("DB_PATH", "").strip() or DB_PATH_POR_DEFECTO
    ruta = Path(valor)
    if not ruta.is_absolute():
        ruta = BASE_DIR / ruta
    return ruta


def duplicado_ventana_dias() -> int:
    valor = os.environ.get("DUPLICADO_VENTANA_DIAS", "").strip()
    dias = int(valor) if valor else DUPLICADO_VENTANA_DIAS_POR_DEFECTO
    if dias < 0:
        raise ValueError("DUPLICADO_VENTANA_DIAS no puede ser negativo")
    return dias
