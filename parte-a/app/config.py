"""Configuración leída de variables de entorno, con un lector sencillo de .env.

Todo se valida de una vez en el arranque (cargar_config): un valor ausente o vacío usa el
defecto; uno no vacío pero inválido hace fallar el arranque con el nombre de la variable.
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# Carpeta parte-a/: las rutas relativas se resuelven contra ella, no contra el cwd.
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_POR_DEFECTO = "data/leads.db"
DUPLICADO_VENTANA_DIAS_POR_DEFECTO = 7
WORKER_INTERVALO_S_POR_DEFECTO = 1.0
TRABAJO_LEASE_S_POR_DEFECTO = 60
TRABAJO_MAX_INTENTOS_POR_DEFECTO = 5
PACIENTES_LIMITE_POR_MIN_POR_DEFECTO = 25
PACIENTES_MAX_PAGINAS_POR_DEFECTO = 5
LLM_PROVEEDORES = ("none", "gemini", "anthropic", "openai")
LLM_TIMEOUT_S_POR_DEFECTO = 10.0
LLM_CONFIANZA_BASURA_POR_DEFECTO = 0.8
WEBHOOK_MAX_BYTES_POR_DEFECTO = 262_144  # 256 KiB (D16)

# Rangos admitidos (mínimo, máximo), ambos incluidos.
RANGO_DUPLICADO_VENTANA_DIAS = (1, 90)
RANGO_WORKER_INTERVALO_S = (0.1, 60.0)
RANGO_TRABAJO_LEASE_S = (10, 3600)
RANGO_TRABAJO_MAX_INTENTOS = (1, 20)
RANGO_PACIENTES_LIMITE_POR_MIN = (1, 30)  # la API admite 30/min por token
RANGO_PACIENTES_MAX_PAGINAS = (1, 20)
RANGO_LLM_TIMEOUT_S = (2.0, 30.0)
RANGO_LLM_CONFIANZA_BASURA = (0.5, 1.0)
RANGO_WEBHOOK_MAX_BYTES = (4096, 4_194_304)  # 4 KiB a 4 MiB
_MODELO = re.compile(r"[A-Za-z0-9._:-]{1,100}")

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
    api_pacientes_url: str | None = None
    # repr=False: el token no aparece si alguien imprime o registra la configuración.
    api_pacientes_token: str | None = field(default=None, repr=False)
    pacientes_limite_por_min: int = PACIENTES_LIMITE_POR_MIN_POR_DEFECTO
    pacientes_max_paginas: int = PACIENTES_MAX_PAGINAS_POR_DEFECTO
    llm_proveedor: str = "none"
    llm_modelo: str | None = None
    llm_api_key: str | None = field(default=None, repr=False)  # nunca en el repr
    llm_timeout_s: float = LLM_TIMEOUT_S_POR_DEFECTO
    llm_confianza_basura: float = LLM_CONFIANZA_BASURA_POR_DEFECTO
    webhook_max_bytes: int = WEBHOOK_MAX_BYTES_POR_DEFECTO

    @property
    def pacientes_configurada(self) -> bool:
        return bool(self.api_pacientes_url and self.api_pacientes_token)


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


def api_pacientes_url(entorno=None) -> str | None:
    entorno = os.environ if entorno is None else entorno
    valor = _valor(entorno, "API_PACIENTES_URL")
    if not valor:
        return None
    partes = urlsplit(valor)
    if partes.scheme not in ("http", "https") or not partes.hostname:
        raise ConfigError("API_PACIENTES_URL debe ser una URL http(s) con host")
    if partes.username or partes.password or partes.query or partes.fragment:
        raise ConfigError("API_PACIENTES_URL no debe llevar credenciales, consulta ni fragmento")
    return valor.rstrip("/")


def api_pacientes_token(entorno=None) -> str | None:
    """El mensaje de error nunca incluye el token."""
    entorno = os.environ if entorno is None else entorno
    valor = _valor(entorno, "API_PACIENTES_TOKEN")
    if not valor:
        return None
    if not all(33 <= ord(c) <= 126 for c in valor):  # ASCII imprimible, sin espacios
        raise ConfigError("API_PACIENTES_TOKEN tiene caracteres no válidos para una cabecera")
    return valor


def _texto_cabecera(entorno, nombre: str) -> str | None:
    """Valor apto para una cabecera HTTP (ASCII imprimible sin espacios). El error no lo muestra."""
    valor = _valor(entorno, nombre)
    if not valor:
        return None
    if not all(33 <= ord(c) <= 126 for c in valor):
        raise ConfigError(f"{nombre} tiene caracteres no válidos para una cabecera")
    return valor


def llm(entorno=None) -> tuple[str, str | None, str | None]:
    """(proveedor, modelo, clave). Modelo y clave solo son obligatorios si el proveedor no es none."""
    entorno = os.environ if entorno is None else entorno
    proveedor = _valor(entorno, "LLM_PROVIDER").lower() or "none"
    if proveedor not in LLM_PROVEEDORES:
        raise ConfigError("LLM_PROVIDER debe ser uno de: " + ", ".join(LLM_PROVEEDORES))
    modelo = _valor(entorno, "LLM_MODEL") or None
    clave = _texto_cabecera(entorno, "LLM_API_KEY")
    if modelo is not None and not _MODELO.fullmatch(modelo):
        raise ConfigError("LLM_MODEL tiene un formato inválido (letras, dígitos, . _ : -)")
    if proveedor != "none":
        if modelo is None:
            raise ConfigError("LLM_MODEL es obligatorio si LLM_PROVIDER no es none")
        if clave is None:
            raise ConfigError("LLM_API_KEY es obligatoria si LLM_PROVIDER no es none")
    return proveedor, modelo, clave


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
        api_pacientes_url=api_pacientes_url(entorno),
        api_pacientes_token=api_pacientes_token(entorno),
        pacientes_limite_por_min=_entero(entorno, "PACIENTES_LIMITE_POR_MIN",
                                         PACIENTES_LIMITE_POR_MIN_POR_DEFECTO,
                                         RANGO_PACIENTES_LIMITE_POR_MIN),
        pacientes_max_paginas=_entero(entorno, "PACIENTES_MAX_PAGINAS",
                                      PACIENTES_MAX_PAGINAS_POR_DEFECTO, RANGO_PACIENTES_MAX_PAGINAS),
        **dict(zip(("llm_proveedor", "llm_modelo", "llm_api_key"), llm(entorno))),
        llm_timeout_s=_decimal(entorno, "LLM_TIMEOUT_S", LLM_TIMEOUT_S_POR_DEFECTO, RANGO_LLM_TIMEOUT_S),
        llm_confianza_basura=_decimal(entorno, "LLM_CONFIANZA_BASURA",
                                      LLM_CONFIANZA_BASURA_POR_DEFECTO, RANGO_LLM_CONFIANZA_BASURA),
        webhook_max_bytes=_entero(entorno, "WEBHOOK_MAX_BYTES", WEBHOOK_MAX_BYTES_POR_DEFECTO,
                                  RANGO_WEBHOOK_MAX_BYTES),
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
