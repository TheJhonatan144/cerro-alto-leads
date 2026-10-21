"""Configuración leída de variables de entorno, con un lector sencillo de .env.

Todo se valida de una vez en el arranque (cargar_config): un valor ausente o vacío usa el
defecto; uno no vacío pero inválido hace fallar el arranque con el nombre de la variable.
"""
import os
import re
from dataclasses import dataclass
from pathlib import Path

# Carpeta parte-a/: las rutas relativas se resuelven contra ella, no contra el cwd.
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_POR_DEFECTO = "data/leads.db"
DUPLICADO_VENTANA_DIAS_POR_DEFECTO = 7
WORKER_INTERVALO_S_POR_DEFECTO = 1.0
TRABAJO_LEASE_S_POR_DEFECTO = 60
TRABAJO_MAX_INTENTOS_POR_DEFECTO = 5

# Rangos admitidos (mínimo, máximo), ambos incluidos.
RANGO_DUPLICADO_VENTANA_DIAS = (1, 90)
RANGO_WORKER_INTERVALO_S = (0.1, 60.0)
RANGO_TRABAJO_LEASE_S = (10, 3600)
RANGO_TRABAJO_MAX_INTENTOS = (1, 20)

_ENTERO = re.compile(r"[+-]?[0-9]+")
_DECIMAL = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)")  # sin exponentes, inf ni nan


class ConfigError(ValueError):
    """Variable de entorno con un valor no vacío pero inválido."""


@dataclass(frozen=True)
class Config:
    db_path: Path
    duplicado_ventana_dias: int
    worker_activo: bool
    worker_intervalo_s: float
    trabajo_lease_s: int
    trabajo_max_intentos: int


def _valor(entorno, nombre: str) -> str:
    return (entorno.get(nombre) or "").strip()


def _en_rango(nombre: str, numero, rango):
    minimo, maximo = rango
    if not minimo <= numero <= maximo:
        raise ConfigError(f"{nombre} debe estar entre {minimo} y {maximo}")
    return numero


def _entero(entorno, nombre: str, defecto: int, rango: tuple[int, int]) -> int:
    valor = _valor(entorno, nombre)
    if not valor:
        return defecto
    # El mensaje nombra la variable pero no repite su valor.
    if not _ENTERO.fullmatch(valor):
        raise ConfigError(f"{nombre} debe ser un número entero")
    return _en_rango(nombre, int(valor), rango)


def _decimal(entorno, nombre: str, defecto: float, rango: tuple[float, float]) -> float:
    valor = _valor(entorno, nombre)
    if not valor:
        return defecto
    if not _DECIMAL.fullmatch(valor):
        raise ConfigError(f"{nombre} debe ser un número (se admiten decimales con punto)")
    return _en_rango(nombre, float(valor), rango)


def db_path(entorno=None) -> Path:
    entorno = os.environ if entorno is None else entorno
    ruta = Path(_valor(entorno, "DB_PATH") or DB_PATH_POR_DEFECTO)
    if not ruta.is_absolute():
        ruta = BASE_DIR / ruta
    return ruta


def duplicado_ventana_dias(entorno=None) -> int:
    entorno = os.environ if entorno is None else entorno
    return _entero(entorno, "DUPLICADO_VENTANA_DIAS", DUPLICADO_VENTANA_DIAS_POR_DEFECTO,
                   RANGO_DUPLICADO_VENTANA_DIAS)


def worker_activo(entorno=None) -> bool:
    entorno = os.environ if entorno is None else entorno
    valor = _valor(entorno, "WORKER_ACTIVO")
    if valor not in ("", "0", "1"):
        raise ConfigError("WORKER_ACTIVO debe ser 0 o 1")
    return valor != "0"


def cargar_config(entorno=None) -> Config:
    entorno = os.environ if entorno is None else entorno
    return Config(
        db_path=db_path(entorno),
        duplicado_ventana_dias=duplicado_ventana_dias(entorno),
        worker_activo=worker_activo(entorno),
        worker_intervalo_s=_decimal(entorno, "WORKER_INTERVALO_S",
                                    WORKER_INTERVALO_S_POR_DEFECTO, RANGO_WORKER_INTERVALO_S),
        trabajo_lease_s=_entero(entorno, "TRABAJO_LEASE_S", TRABAJO_LEASE_S_POR_DEFECTO,
                                RANGO_TRABAJO_LEASE_S),
        trabajo_max_intentos=_entero(entorno, "TRABAJO_MAX_INTENTOS",
                                     TRABAJO_MAX_INTENTOS_POR_DEFECTO, RANGO_TRABAJO_MAX_INTENTOS),
    )


def rutas_dotenv() -> list[Path]:
    """Primero ./.env y luego ../.env, relativos al directorio actual."""
    return [Path.cwd() / ".env", Path.cwd().parent / ".env"]


def cargar_env(rutas=None, entorno=None) -> Path | None:
    """Carga el primer .env que exista. Líneas CLAVE=VALOR; ignora comentarios y vacías.

    No pisa variables ya definidas en el entorno. Devuelve la ruta cargada o None.
    """
    entorno = os.environ if entorno is None else entorno
    for ruta in rutas_dotenv() if rutas is None else rutas:
        if not ruta.is_file():
            continue
        for linea in ruta.read_text(encoding="utf-8-sig").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, valor = (parte.strip() for parte in linea.split("=", 1))
            if clave.startswith("export "):
                clave = clave[len("export "):].strip()
            if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
                valor = valor[1:-1]
            if clave and clave not in entorno:
                entorno[clave] = valor
        return ruta
    return None
