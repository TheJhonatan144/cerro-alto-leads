"""Prueba de humo contra la API de pacientes real y, con --llm, contra el LLM configurado.

PARA EL USUARIO: la ejecuta el autor a mano, con su .env. Nunca forma parte de los tests.

Uso (desde parte-a/, con el venv activo):
    python scripts/prueba_humo_real.py                 # solo la API de pacientes
    python scripts/prueba_humo_real.py --llm           # y una petición al LLM
    python scripts/prueba_humo_real.py --telefono +593990000001

Usa los clientes de la propia app (ClientePacientes y ClienteLLM) y hace como máximo:
- 1 búsqueda de pacientes por un teléfono FICTICIO y, solo si hay exactamente un paciente,
  1 petición de sus tratamientos (nunca más de 3 peticiones a la API de pacientes; sin
  reintentos ni paginación);
- con --llm, UNA petición al proveedor configurado con un mensaje ficticio ya enmascarado.

Imprime solo códigos HTTP, nombres de claves del JSON (nunca valores), si el validador aceptó
la salida del LLM y la ruta resultante. Nunca imprime tokens, claves, URLs ni cuerpos.
Sale con 0 si todo respondió 200 (y el validador aceptó), 1 si no, y 2 si la configuración
es inválida.
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # para importar app desde scripts/

import httpx  # noqa: E402

from app import config, reglas  # noqa: E402
from app.clasificacion import SalidaInvalida, decidir_ruta, validar_salida_llm  # noqa: E402
from app.enmascarar import contenido_para_llm  # noqa: E402
from app.errores import Reprogramar  # noqa: E402
from app.llm import ClienteLLM, cargar_prompt, construir_mensaje_usuario  # noqa: E402
from app.pacientes import ApiNoDisponible, ClientePacientes, NoEncontrado, Presupuesto  # noqa: E402

TELEFONO_FICTICIO = "+593990000000"
# Ficticio. Lleva un número para comprobar que se enmascara antes de salir.
MENSAJE_FICTICIO = "Hola, me interesa una limpieza dental. Mi número es 0990000000."
SERVICIO_FICTICIO = "Limpieza"
MAX_PETICIONES_PACIENTES = 3
PRESUPUESTO_S = 20.0


class LimiteDePeticiones(RuntimeError):
    pass


class Contador(httpx.BaseTransport):
    """Envuelve el transporte real: cuenta las peticiones, anota el código HTTP de cada
    respuesta y corta antes de superar el máximo."""

    def __init__(self, interno: httpx.BaseTransport, maximo: int):
        self.interno, self.maximo, self.codigos = interno, maximo, []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if len(self.codigos) >= self.maximo:
            raise LimiteDePeticiones("máximo de peticiones de la prueba alcanzado")
        self.codigos.append(None)  # cuenta aunque falle la red
        r = self.interno.handle_request(request)
        self.codigos[-1] = r.status_code
        return r

    def close(self) -> None:
        self.interno.close()

    def ultimo(self) -> str:
        return f"HTTP {self.codigos[-1]}" if self.codigos and self.codigos[-1] else "sin respuesta"


def _claves(x) -> str:
    return ", ".join(sorted(map(str, x))) if isinstance(x, dict) else f"(no es un objeto: {type(x).__name__})"


def _pacientes(cfg, telefono: str, transporte) -> bool:
    if not cfg.pacientes_configurada:
        print("API de pacientes: no configurada (API_PACIENTES_URL o API_PACIENTES_TOKEN vacías); se omite")
        return True
    contador = Contador(transporte or httpx.HTTPTransport(), MAX_PETICIONES_PACIENTES)
    cliente = ClientePacientes(cfg.api_pacientes_url, cfg.api_pacientes_token, max_paginas=1,
                               limite_por_min=cfg.pacientes_limite_por_min, transport=contador)
    presupuesto = Presupuesto(PRESUPUESTO_S)
    try:
        try:  # cliente.get: una sola petición, sin el reintento ni la paginación de la Sesion
            datos = cliente.get("/v1/pacientes", {"telefono": telefono}, presupuesto)
        except (ApiNoDisponible, NoEncontrado, Reprogramar) as e:
            print(f"búsqueda: {contador.ultimo()} ({getattr(e, 'codigo', type(e).__name__)})")
            return False
        print(f"búsqueda: {contador.ultimo()}")
        print(f"  claves: {_claves(datos)}")
        elementos = datos.get("data") if isinstance(datos, dict) else None
        if isinstance(elementos, list):
            print(f"  elementos: {len(elementos)}")
            if elementos:
                print(f"  claves de cada elemento: {_claves(elementos[0])}")
        unico = elementos[0] if isinstance(elementos, list) and len(elementos) == 1 else None
        if not (isinstance(unico, dict) and isinstance(unico.get("id"), str) and unico["id"]):
            print("tratamientos: se omite (no hay exactamente un paciente)")
            return True
        try:
            datos = cliente.get(f"/v1/pacientes/{quote(unico['id'], safe='')}/tratamientos", {}, presupuesto)
        except (ApiNoDisponible, NoEncontrado, Reprogramar) as e:
            print(f"tratamientos: {contador.ultimo()} ({getattr(e, 'codigo', type(e).__name__)})")
            return False
        print(f"tratamientos: {contador.ultimo()}")
        print(f"  claves: {_claves(datos)}")
        elementos = datos.get("data") if isinstance(datos, dict) else None
        if isinstance(elementos, list) and elementos:
            print(f"  claves de cada elemento: {_claves(elementos[0])}")
        return True
    finally:
        cliente.cerrar()


def _llm(cfg, transporte) -> bool:
    if cfg.llm_proveedor == "none":
        print("LLM: LLM_PROVIDER=none; se omite")
        return True
    contador = Contador(transporte or httpx.HTTPTransport(), 1)
    cliente = ClienteLLM(cfg.llm_proveedor, cfg.llm_modelo, cfg.llm_api_key, timeout_s=cfg.llm_timeout_s,
                         transport=contador)
    resultado = reglas.evaluar(MENSAJE_FICTICIO, servicio=SERVICIO_FICTICIO)
    contenido = contenido_para_llm(MENSAJE_FICTICIO, servicio=SERVICIO_FICTICIO)
    try:
        try:  # _post: UNA petición (clasificar() reintentaría una vez)
            texto = cliente._post(cargar_prompt().texto, construir_mensaje_usuario(contenido),
                                  cliente.presupuesto(PRESUPUESTO_S))
        except (ApiNoDisponible, Reprogramar) as e:
            print(f"LLM: {contador.ultimo()} ({getattr(e, 'codigo', type(e).__name__)})")
            print(f"ruta: {decidir_ruta(resultado, None, cfg.llm_confianza_basura).ruta} (solo reglas)")
            return False
        print(f"LLM: {contador.ultimo()} (proveedor {cfg.llm_proveedor})")
        try:
            print(f"  claves de la salida: {_claves(json.loads(texto))}")
        except ValueError:
            print("  claves de la salida: (no es JSON)")
        try:
            salida = validar_salida_llm(texto)
        except SalidaInvalida as e:
            print(f"validador: rechazada ({e.codigo})")
            print(f"ruta: {decidir_ruta(resultado, None, cfg.llm_confianza_basura).ruta} (solo reglas)")
            return False
        print("validador: aceptada")
        print(f"ruta: {decidir_ruta(resultado, salida, cfg.llm_confianza_basura).ruta}")
        return True
    finally:
        cliente.cerrar()


def main(argv=None, *, transporte_pacientes=None, transporte_llm=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--telefono", default=TELEFONO_FICTICIO, help="teléfono FICTICIO para la búsqueda")
    p.add_argument("--llm", action="store_true", help="hace también UNA petición al LLM configurado")
    a = p.parse_args(argv)
    config.cargar_env()
    try:
        cfg = config.cargar_config()
    except config.ConfigError as e:
        print(f"configuración inválida: {e}", file=sys.stderr)  # el mensaje nombra la variable, sin valor
        return 2
    ok = _pacientes(cfg, a.telefono, transporte_pacientes)
    if a.llm:
        ok = _llm(cfg, transporte_llm) and ok
    else:
        print("LLM: se omite (sin --llm)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
