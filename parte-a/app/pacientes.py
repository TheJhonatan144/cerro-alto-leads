"""Cliente de la API de pacientes: httpx, limitador local, presupuesto de tiempo y validación.

Nunca registra URLs (llevan teléfono y correo) ni el token. Toda respuesta que no sirve
termina en ApiNoDisponible (con un código corto), NoEncontrado (404) o Reprogramar (429 o
cupo local agotado); nunca en una excepción sin controlar.
"""
import json
import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

import httpx

from app import normalizar as n
from app.errores import Reprogramar

# httpx registra cada petición con su URL en INFO: teléfonos y correos acabarían en los logs.
for _nombre in ("httpx", "httpcore"):
    logging.getLogger(_nombre).setLevel(logging.WARNING)

TIMEOUT_CONEXION_S = 3.0
TIMEOUT_LECTURA_S = 5.0
RETRY_AFTER_MAX_S = 120  # un 429 que pide más espera degrada (no se reprograma)
RETRY_AFTER_MAX_DIGITOS = 6
MAX_CUERPO_BYTES = 256 * 1024
TAM_PAGINA = 3  # máximo de elementos por página según el contrato
MIN_PARA_REINTENTO_S = 1.0  # presupuesto mínimo para el único reintento corto (D5)
VENTANA_LIMITADOR_S = 60.0

SEDES = ("norte", "valle")
ESTADOS_PACIENTE = ("activo", "inactivo")
ESTADOS_TRATAMIENTO = ("pendiente", "en_curso", "completado", "cancelado")
# Solo garantiza que el id es seguro dentro de la URL de tratamientos; no asume un formato
# concreto (el real no está verificado). Se usa con fullmatch.
PATRON_PACIENTE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
PATRON_TIPO = re.compile(r"[a-z0-9_ ]{1,40}")
PATRON_FECHA = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class ApiNoDisponible(Exception):
    """La API no dio una respuesta utilizable. 'codigo' es corto y sin datos personales."""

    def __init__(self, codigo: str, transitorio: bool = False):
        super().__init__(codigo)
        self.codigo = codigo
        self.transitorio = transitorio  # timeout, red o 5xx: admite el reintento corto


class NoEncontrado(Exception):
    """404 de la API."""


class Limitador:
    """Ventana deslizante de N solicitudes por minuto. Sin cupo lanza Reprogramar con lo que
    falta para liberar uno; nunca duerme."""

    def __init__(self, por_minuto: int, reloj=time.monotonic):
        self.por_minuto = por_minuto
        self._reloj = reloj
        self._marcas: deque[float] = deque()
        self._cerrojo = threading.Lock()

    def tomar(self) -> None:
        with self._cerrojo:
            ahora = self._reloj()
            while self._marcas and self._marcas[0] <= ahora - VENTANA_LIMITADOR_S:
                self._marcas.popleft()
            if len(self._marcas) >= self.por_minuto:
                raise Reprogramar(math.ceil(self._marcas[0] + VENTANA_LIMITADOR_S - ahora))
            self._marcas.append(ahora)


class Presupuesto:
    """Tiempo total de una etapa (reloj monotónico). Cada petición usa min(lectura_max, restante)
    de lectura y min(conexion_max, restante) de conexión."""

    def __init__(self, total_s: float, reloj=time.monotonic, *,
                 lectura_max: float = TIMEOUT_LECTURA_S, conexion_max: float = TIMEOUT_CONEXION_S):
        self._reloj = reloj
        self._fin = reloj() + total_s
        self.lectura_max = lectura_max
        self.conexion_max = conexion_max

    def restante(self) -> float:
        return self._fin - self._reloj()

    def timeout(self) -> httpx.Timeout:
        restante = self.restante()
        if restante <= 0:
            raise ApiNoDisponible("tiempo_agotado")
        return httpx.Timeout(min(self.lectura_max, restante),
                             connect=min(self.conexion_max, restante))


@dataclass(frozen=True)
class Paciente:
    """Solo lo necesario: el nombre se reduce a su clave de comparación y no se guarda.
    Un campo inválido queda en None y su advertencia en 'advertencias' (nunca el texto)."""
    id: str
    clave_nombre: str
    sede_habitual: str | None
    ultima_visita: str | None
    estado: str | None
    advertencias: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tratamiento:
    tipo: str | None  # normalizado; None si no cumple PATRON_TIPO
    estado: str


def segundos_retry_after(valor: str | None, maximo: int) -> int | None:
    """Segundos (1..maximo) o None si falta, es inválido o supera el tope (se degrada).
    Solo dígitos y con longitud acotada: nunca convierte enteros enormes."""
    texto = (valor or "").strip()
    if not texto or len(texto) > RETRY_AFTER_MAX_DIGITOS or not (texto.isascii() and texto.isdigit()):
        return None
    segundos = int(texto)
    return max(1, segundos) if segundos <= maximo else None


def _retry_after(valor: str | None) -> int | None:
    return segundos_retry_after(valor, RETRY_AFTER_MAX_S)


def _forma():
    return ApiNoDisponible("forma_inesperada")


def decodificar_json(cuerpo: bytes):
    try:
        return json.loads(cuerpo)
    except (ValueError, RecursionError):  # incluye UTF-8 inválido y JSON anidado sin fin
        raise _forma() from None


def _fecha(valor) -> str | None:
    if not isinstance(valor, str) or not PATRON_FECHA.fullmatch(valor):
        return None
    try:
        date.fromisoformat(valor)
    except ValueError:
        return None
    return valor


def _validar_paciente(x) -> Paciente:
    if not isinstance(x, dict) or not isinstance(x.get("nombre_completo"), str):
        raise _forma()
    # Sin un id válido no se puede identificar ni pedir tratamientos: la respuesta no sirve.
    if not isinstance(x.get("id"), str) or not PATRON_PACIENTE_ID.fullmatch(x["id"]):
        raise _forma()
    advertencias = []
    sede = x.get("sede_habitual")
    if sede is not None and sede not in SEDES:
        sede = None
        advertencias.append("sede_invalida")
    estado = x.get("estado")
    if estado not in ESTADOS_PACIENTE:
        estado = None
        advertencias.append("estado_paciente_invalido")
    visita = x.get("ultima_visita")
    if visita is not None and _fecha(visita) is None:
        visita = None
        advertencias.append("ultima_visita_invalida")
    return Paciente(id=x["id"], clave_nombre=n.clave_nombre(x["nombre_completo"]),
                    sede_habitual=sede, ultima_visita=visita, estado=estado,
                    advertencias=tuple(advertencias))


def _validar_tratamiento(x) -> Tratamiento:
    if not isinstance(x, dict) or not isinstance(x.get("tipo"), str) or not x["tipo"].strip():
        raise _forma()
    if x.get("estado") not in ESTADOS_TRATAMIENTO:
        raise _forma()
    tipo = n.clave_nombre(x["tipo"])  # minúsculas, sin tildes, espacios colapsados
    return Tratamiento(tipo=tipo if PATRON_TIPO.fullmatch(tipo) else None, estado=x["estado"])


def _validar_pagina(cuerpo, validar):
    """(elementos, next_cursor, advertencia o None)."""
    if not isinstance(cuerpo, dict) or not isinstance(cuerpo.get("data"), list):
        raise _forma()
    advertencia = None
    if "next_cursor" not in cuerpo:
        # Solo se acepta como último tramo: una página llena sin cursor es ambigua.
        if len(cuerpo["data"]) >= TAM_PAGINA:
            raise _forma()
        advertencia = "next_cursor_ausente"
    siguiente = cuerpo.get("next_cursor")
    if siguiente is not None and (not isinstance(siguiente, str) or not siguiente):
        raise _forma()
    return [validar(x) for x in cuerpo["data"]], siguiente, advertencia


def leer_acotado(r: httpx.Response, presupuesto: Presupuesto, max_bytes: int = MAX_CUERPO_BYTES) -> bytes:
    """Lee el cuerpo por trozos comprobando el presupuesto total y el tamaño máximo.
    La reutiliza el cliente del LLM (app/llm.py)."""
    largo = r.headers.get("Content-Length", "")
    if largo.isdigit() and (len(largo) > 9 or int(largo) > max_bytes):
        raise ApiNoDisponible("respuesta_demasiado_grande")
    cuerpo = bytearray()
    for trozo in r.iter_bytes():  # ya descomprimido: el tope también vale para gzip
        cuerpo += trozo
        if len(cuerpo) > max_bytes:
            raise ApiNoDisponible("respuesta_demasiado_grande")
        if presupuesto.restante() <= 0:
            raise ApiNoDisponible("timeout", transitorio=True)
    return bytes(cuerpo)


class ClientePacientes:
    """Un cliente por proceso: comparte el limitador entre trabajos."""

    def __init__(self, url: str, token: str, *, limite_por_min: int = 25, max_paginas: int = 5,
                 transport: httpx.BaseTransport | None = None, reloj=time.monotonic):
        self.limitador = Limitador(limite_por_min, reloj)
        self.max_paginas = max_paginas
        self.reloj = reloj
        self._http = httpx.Client(
            base_url=url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=httpx.Timeout(TIMEOUT_LECTURA_S, connect=TIMEOUT_CONEXION_S),
            follow_redirects=False,
            trust_env=False,  # sin proxies ni .netrc del entorno
            transport=transport,
        )

    def __repr__(self) -> str:  # nunca mostrar cabeceras
        return "<ClientePacientes>"

    def cerrar(self) -> None:
        self._http.close()

    def sesion(self, presupuesto_s: float) -> "Sesion":
        return Sesion(self, Presupuesto(presupuesto_s, self.reloj))

    def get(self, ruta: str, params: dict, presupuesto: Presupuesto):
        timeout = presupuesto.timeout()
        self.limitador.tomar()
        try:
            # En streaming: httpx limita cada lectura, no el total; leer_acotado vigila el total.
            with self._http.stream("GET", ruta, params=params, timeout=timeout) as r:
                if r.status_code == 200:
                    return decodificar_json(leer_acotado(r, presupuesto))
                estado, retry_after = r.status_code, r.headers.get("Retry-After")
        # "from None": el mensaje encadenado podría incluir la URL con teléfono y correo.
        except httpx.TimeoutException:
            raise ApiNoDisponible("timeout", transitorio=True) from None
        except httpx.DecodingError:
            raise _forma() from None
        except httpx.HTTPError:
            raise ApiNoDisponible("error_de_red", transitorio=True) from None
        if estado == 429:
            segundos = _retry_after(retry_after)
            if segundos is None:  # pide más de 120 s o no se entiende: no se reintenta antes
                raise ApiNoDisponible("limite_de_uso")
            raise Reprogramar(segundos)
        if estado in (401, 403):
            raise ApiNoDisponible("credenciales")
        if estado == 404:
            raise NoEncontrado()
        if estado == 400:
            raise ApiNoDisponible("solicitud_rechazada")
        if estado >= 500:
            raise ApiNoDisponible("error_del_servidor", transitorio=True)
        raise ApiNoDisponible("respuesta_inesperada")  # p. ej. una redirección no seguida


class Sesion:
    """Una ejecución de la etapa: presupuesto propio y un único reintento corto (D5)."""

    def __init__(self, cliente: ClientePacientes, presupuesto: Presupuesto):
        self.cliente = cliente
        self.presupuesto = presupuesto
        self.reintentos = 1
        self.solicitudes = 0

    def _get(self, ruta, params):
        try:
            self.solicitudes += 1
            return self.cliente.get(ruta, params, self.presupuesto)
        except ApiNoDisponible as e:
            if not (e.transitorio and self.reintentos > 0
                    and self.presupuesto.restante() >= MIN_PARA_REINTENTO_S):
                raise
            self.reintentos -= 1
            self.solicitudes += 1
            return self.cliente.get(ruta, params, self.presupuesto)

    def _paginar(self, ruta, params, validar):
        """(elementos, incompleto, advertencias). incompleto: tope alcanzado con más páginas."""
        elementos, cursor, advertencias = [], None, []
        for _ in range(self.cliente.max_paginas):
            cuerpo = self._get(ruta, {**params, "cursor": cursor} if cursor else params)
            pagina, cursor, advertencia = _validar_pagina(cuerpo, validar)
            elementos += pagina
            if advertencia and advertencia not in advertencias:
                advertencias.append(advertencia)
            if cursor is None:
                return elementos, False, advertencias
        return elementos, True, advertencias

    def buscar_pacientes(self, telefono: str | None, email: str | None):
        params = {k: v for k, v in (("telefono", telefono), ("email", email)) if v}
        if not params:
            raise ValueError("se necesita teléfono o correo")
        try:
            return self._paginar("/v1/pacientes", params, _validar_paciente)
        except NoEncontrado:
            raise ApiNoDisponible("respuesta_inesperada") from None

    def tratamientos(self, paciente_id: str):
        """Lanza NoEncontrado si la API responde 404."""
        ruta = f"/v1/pacientes/{quote(paciente_id, safe='')}/tratamientos"
        return self._paginar(ruta, {}, _validar_tratamiento)
