"""Configuración leída de variables de entorno, con un lector sencillo de .env.

Todo se valida de una vez en el arranque (cargar_config): un valor ausente o vacío usa el
defecto; uno no vacío pero inválido hace fallar el arranque con el nombre de la variable.
"""
import os
import re
from dataclasses import dataclass, field
from datetime import date
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
CANALES = ("archivo", "telegram", "n8n")
MAX_CANALES = 3
AVISOS_ARCHIVO_POR_DEFECTO = "data/avisos.jsonl"
ERRORES_ARCHIVO_POR_DEFECTO = "data/errores.jsonl"
AVISO_PLAZO_S_POR_DEFECTO = 300
ERRORES_VENTANA_S_POR_DEFECTO = 900
PENDIENTE_ANTIGUO_S_POR_DEFECTO = 900
VIGILANCIA_CADA_S_POR_DEFECTO = 60

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
RANGO_AVISO_PLAZO_S = (60, 3600)
RANGO_ERRORES_VENTANA_S = (60, 86400)
RANGO_PENDIENTE_ANTIGUO_S = (60, 86400)
RANGO_VIGILANCIA_CADA_S = (10, 3600)
_TOKEN_TELEGRAM = re.compile(r"[A-Za-z0-9:_-]{20,200}")  # va en la URL: sin / ? # % ni espacios
_CHAT_TELEGRAM = re.compile(r"-?\d{1,20}|@[A-Za-z0-9_]{5,32}")
_FECHA = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
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
    avisos_canal: str = "archivo"  # lista separada por comas, ya normalizada
    avisos_archivo: Path = BASE_DIR / AVISOS_ARCHIVO_POR_DEFECTO
    aviso_plazo_s: int = AVISO_PLAZO_S_POR_DEFECTO
    feriados: tuple[date, ...] = ()
    agenda_url: str | None = None
    errores_canal: str = "archivo"
    errores_archivo: Path = BASE_DIR / ERRORES_ARCHIVO_POR_DEFECTO
    errores_ventana_s: int = ERRORES_VENTANA_S_POR_DEFECTO
    pendiente_antiguo_s: int = PENDIENTE_ANTIGUO_S_POR_DEFECTO
    vigilancia_cada_s: int = VIGILANCIA_CADA_S_POR_DEFECTO
    # Secretos y destinos: repr=False (nunca en logs ni en el repr de la configuración).
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = field(default=None, repr=False)
    n8n_avisos_url: str | None = field(default=None, repr=False)
    n8n_errores_url: str | None = field(default=None, repr=False)
    n8n_secreto: str | None = field(default=None, repr=False)

    @property
    def avisos_canales(self) -> tuple[str, ...]:
        return tuple(self.avisos_canal.split(","))

    @property
    def errores_canales(self) -> tuple[str, ...]:
        return tuple(self.errores_canal.split(","))

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


def _ruta(entorno, nombre: str, defecto: str) -> Path:
    ruta = Path(_valor(entorno, nombre) or defecto)
    return ruta if ruta.is_absolute() else BASE_DIR / ruta


def canales(entorno, nombre: str) -> str:
    """Lista de canales separada por comas, de {archivo, telegram, n8n}; máx. 3, sin repetidos."""
    valor = _valor(entorno, nombre).lower() or "archivo"
    lista = [c.strip() for c in valor.split(",")]
    if any(c not in CANALES for c in lista):
        raise ConfigError(f"{nombre} solo admite: " + ", ".join(CANALES))
    if len(lista) > MAX_CANALES or len(set(lista)) != len(lista):
        raise ConfigError(f"{nombre} admite como mucho {MAX_CANALES} canales, sin repetirlos")
    return ",".join(lista)


def avisos_canal(entorno=None) -> str:
    entorno = os.environ if entorno is None else entorno
    return canales(entorno, "AVISOS_CANAL")


def url_webhook(entorno, nombre: str) -> str | None:
    """URL http(s) con host y sin credenciales. El error nunca muestra el valor."""
    valor = _valor(entorno, nombre)
    if not valor:
        return None
    partes = urlsplit(valor)
    if partes.scheme not in ("http", "https") or not partes.hostname or any(c.isspace() for c in valor):
        raise ConfigError(f"{nombre} debe ser una URL http(s) con host")
    if partes.username or partes.password:
        raise ConfigError(f"{nombre} no debe llevar credenciales embebidas")
    return valor


def _patron(entorno, nombre: str, patron: re.Pattern, explicacion: str) -> str | None:
    valor = _valor(entorno, nombre)
    if not valor:
        return None
    if not patron.fullmatch(valor):
        raise ConfigError(f"{nombre} {explicacion}")
    return valor


def feriados(entorno=None) -> tuple[date, ...]:
    """Fechas ISO (AAAA-MM-DD) separadas por coma. El error nombra la variable, no el valor."""
    entorno = os.environ if entorno is None else entorno
    valor = _valor(entorno, "FERIADOS")
    if not valor:
        return ()
    fechas = []
    for parte in valor.split(","):
        parte = parte.strip()
        if not _FECHA.fullmatch(parte):
            raise ConfigError("FERIADOS debe ser una lista de fechas AAAA-MM-DD separadas por coma")
        try:
            fechas.append(date.fromisoformat(parte))
        except ValueError:
            raise ConfigError("FERIADOS contiene una fecha que no existe") from None
    return tuple(sorted(set(fechas)))


def agenda_url(entorno=None) -> str | None:
    entorno = os.environ if entorno is None else entorno
    valor = _valor(entorno, "AGENDA_URL")
    if not valor:
        return None
    partes = urlsplit(valor)
    if partes.scheme not in ("http", "https") or not partes.hostname:
        raise ConfigError("AGENDA_URL debe ser una URL http(s) con host")
    if partes.username or partes.password:
        raise ConfigError("AGENDA_URL no debe llevar credenciales")
    if any(c.isspace() for c in valor):
        raise ConfigError("AGENDA_URL no debe contener espacios")
    return valor


def cargar_config(entorno=None) -> Config:
    entorno = os.environ if entorno is None else entorno
    cfg = Config(
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
        avisos_canal=avisos_canal(entorno),
        avisos_archivo=_ruta(entorno, "AVISOS_ARCHIVO", AVISOS_ARCHIVO_POR_DEFECTO),
        aviso_plazo_s=_entero(entorno, "AVISO_PLAZO_S", AVISO_PLAZO_S_POR_DEFECTO, RANGO_AVISO_PLAZO_S),
        feriados=feriados(entorno),
        agenda_url=agenda_url(entorno),
        errores_canal=canales(entorno, "ERRORES_CANAL"),
        errores_archivo=_ruta(entorno, "ERRORES_ARCHIVO", ERRORES_ARCHIVO_POR_DEFECTO),
        errores_ventana_s=_entero(entorno, "ERRORES_VENTANA_S", ERRORES_VENTANA_S_POR_DEFECTO,
                                  RANGO_ERRORES_VENTANA_S),
        pendiente_antiguo_s=_entero(entorno, "PENDIENTE_ANTIGUO_S", PENDIENTE_ANTIGUO_S_POR_DEFECTO,
                                    RANGO_PENDIENTE_ANTIGUO_S),
        vigilancia_cada_s=_entero(entorno, "VIGILANCIA_CADA_S", VIGILANCIA_CADA_S_POR_DEFECTO,
                                  RANGO_VIGILANCIA_CADA_S),
        telegram_bot_token=_patron(entorno, "TELEGRAM_BOT_TOKEN", _TOKEN_TELEGRAM,
                                   "debe tener entre 20 y 200 caracteres: letras, dígitos, ':', '_' o '-'"),
        telegram_chat_id=_patron(entorno, "TELEGRAM_CHAT_ID", _CHAT_TELEGRAM,
                                 "debe ser un id numérico o un @canal de 5 a 32 caracteres"),
        n8n_avisos_url=url_webhook(entorno, "N8N_AVISOS_URL"),
        n8n_errores_url=url_webhook(entorno, "N8N_ERRORES_URL"),
        n8n_secreto=_texto_cabecera(entorno, "N8N_SECRETO"),
    )
    _exigir_configuracion_de_canales(cfg)
    return cfg


def _exigir_configuracion_de_canales(cfg: Config) -> None:
    """Un canal elegido sin su configuración impide arrancar (mensaje sin valores)."""
    for variable, lista in (("AVISOS_CANAL", cfg.avisos_canales), ("ERRORES_CANAL", cfg.errores_canales)):
        if "telegram" in lista:
            if not cfg.telegram_bot_token:
                raise ConfigError(f"{variable} incluye telegram pero falta TELEGRAM_BOT_TOKEN")
            if not cfg.telegram_chat_id:
                raise ConfigError(f"{variable} incluye telegram pero falta TELEGRAM_CHAT_ID")
    if "n8n" in cfg.avisos_canales and not cfg.n8n_avisos_url:
        raise ConfigError("AVISOS_CANAL incluye n8n pero falta N8N_AVISOS_URL")
    if "n8n" in cfg.errores_canales and not cfg.n8n_errores_url:
        raise ConfigError("ERRORES_CANAL incluye n8n pero falta N8N_ERRORES_URL")


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
