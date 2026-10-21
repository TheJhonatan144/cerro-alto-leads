"""Envía los payloads de ejemplo al sistema y comprueba el estado y la ruta de cada uno.

Uso (con el servidor ya arrancado):
    python scripts/verificar_payloads.py
    python scripts/verificar_payloads.py --url http://127.0.0.1:8000 --pausa 1 --timeout 60

Para cada archivo de payloads/ (en orden): POST a {url}/webhook/leads, y después consulta
GET {url}/leads/estado?event_id=... hasta que el evento deje de estar 'pendiente' (máximo
--timeout s por evento). Compara estado y ruta con tests/esperado_payloads.json, que supone
LLM_PROVIDER=none. Imprime solo archivo, estado y ruta (nunca datos del lead). Sale con
código 0 si todo coincide, 1 si algo no coincide o falla, y 2 si los argumentos son inválidos.

Solo biblioteca estándar: Python 3.10 o superior.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PARTE_A = Path(__file__).resolve().parents[1]
CARPETA = PARTE_A.parent / "payloads"
ESPERADO = PARTE_A / "tests" / "esperado_payloads.json"
TIMEOUT_PETICION_S = 10  # como el conector

# Sin proxies del entorno: las peticiones van directas a la URL indicada.
_abrir = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def _peticion(url: str, cuerpo: bytes | None = None) -> tuple[int | None, bytes]:
    req = urllib.request.Request(url, data=cuerpo, method="POST" if cuerpo is not None else "GET",
                                 headers={"Content-Type": "application/json"} if cuerpo is not None else {})
    try:
        with _abrir(req, timeout=TIMEOUT_PETICION_S) as r:
            return r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        return e.code, e.read(65536)
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None, b""


def _esperar_estado(base: str, event_id: str, timeout: float) -> tuple[str | None, dict | None]:
    """Consulta /leads/estado hasta que no esté 'pendiente'. Devuelve (error, estado)."""
    url = f"{base}/leads/estado?" + urllib.parse.urlencode({"event_id": event_id})
    limite = time.monotonic() + timeout
    ultimo = None
    while True:
        codigo, cuerpo = _peticion(url)
        if codigo == 200:
            try:
                ultimo = json.loads(cuerpo)
            except ValueError:
                return "respuesta ilegible", None
            if ultimo.get("estado") != "pendiente":
                return None, ultimo
        elif codigo not in (404, 503):  # 404: aún no visible; 503: base ocupada
            return (f"HTTP {codigo}" if codigo else "sin respuesta"), None
        if time.monotonic() >= limite:
            return ("sigue pendiente" if ultimo else f"HTTP {codigo}" if codigo else "sin respuesta"), None
        time.sleep(0.25)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8000", help="URL base del sistema")
    p.add_argument("--carpeta", default=str(CARPETA), help="carpeta de payloads")
    p.add_argument("--esperado", default=str(ESPERADO), help="JSON con el estado y la ruta esperados")
    p.add_argument("--pausa", type=float, default=1.0, help="segundos entre un envío y el siguiente")
    p.add_argument("--timeout", type=float, default=60.0, help="segundos máximos de espera por evento")
    a = p.parse_args(argv)

    base = a.url.rstrip("/")
    partes = urllib.parse.urlsplit(base)
    if partes.scheme not in ("http", "https") or not partes.hostname:
        print("--url debe ser http(s)://host[:puerto]", file=sys.stderr)
        return 2
    try:
        esperado = json.loads(Path(a.esperado).read_text(encoding="utf-8"))["archivos"]
    except (OSError, ValueError, KeyError):
        print("no se pudo leer el archivo de esperados", file=sys.stderr)
        return 2
    archivos = sorted(Path(a.carpeta).glob("*.json"))
    if not archivos:
        print("no hay payloads .json en la carpeta", file=sys.stderr)
        return 2

    # 1) Envío en orden, como el conector.
    enviados: list[tuple[Path, str | None, str | None]] = []  # (archivo, event_id, error)
    for f in archivos:
        cuerpo = f.read_bytes()
        try:
            event_id = json.loads(cuerpo).get("event_id")
        except (ValueError, AttributeError):
            event_id = None
        codigo, _ = _peticion(f"{base}/webhook/leads", cuerpo)
        error = None if codigo in (200, 202) else (f"HTTP {codigo}" if codigo else "sin respuesta")
        enviados.append((f, event_id if isinstance(event_id, str) else None, error))
        if a.pausa > 0:
            time.sleep(a.pausa)

    # 2) Estado final de cada uno y comparación.
    coinciden = 0
    print("archivo\testado\truta\tresultado")
    for f, event_id, error in enviados:
        esp = esperado.get(f.name)
        if error is None and event_id is None:
            error = "sin event_id"
        if error is None:
            error, estado = _esperar_estado(base, event_id, a.timeout)
        if error is not None:
            print(f"{f.name}\t{error}")
            continue
        real = (estado.get("estado"), estado.get("ruta"))
        ok = esp is not None and real == (esp.get("estado"), esp.get("ruta"))
        coinciden += ok
        detalle = "OK" if ok else (f"NO COINCIDE (esperado {esp.get('estado')}/{esp.get('ruta') or '-'})"
                                   if esp else "NO COINCIDE (sin esperado)")
        print(f"{f.name}\t{real[0]}\t{real[1] or '-'}\t{detalle}")
    print(f"{coinciden}/{len(enviados)} coinciden")
    return 0 if coinciden == len(enviados) else 1


if __name__ == "__main__":
    sys.exit(main())
