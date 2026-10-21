"""Bloque 6.2: esperado_payloads.json y scripts/verificar_payloads.py (contra un servidor en loopback)."""
import importlib.util
import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.trabajador import procesar_pendientes

RAIZ = Path(__file__).resolve().parents[2]
PAYLOADS = RAIZ / "payloads"
ESPERADO = Path(__file__).with_name("esperado_payloads.json")
SCRIPT = RAIZ / "parte-a" / "scripts" / "verificar_payloads.py"
DATOS_PERSONALES = ("carla", "mendoza", "0991234567", "+593", "@example", "@ganadinero")


def cargar_script():
    spec = importlib.util.spec_from_file_location("verificar_payloads", SCRIPT)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def esperado() -> dict:
    return json.loads(ESPERADO.read_text(encoding="utf-8"))["archivos"]


def test_esperado_cubre_todos_los_payloads():
    assert sorted(esperado()) == sorted(f.name for f in PAYLOADS.glob("*.json"))


def test_esperado_coincide_con_el_procesamiento_real(tmp_path):
    ruta = tmp_path / "leads.db"
    with TestClient(create_app(ruta)) as c:  # LLM_PROVIDER=none y API sin configurar (conftest)
        for f in sorted(PAYLOADS.glob("*.json")):
            assert c.post("/webhook/leads", content=f.read_bytes()).status_code in (200, 202)
        procesar_pendientes(ruta)
        for nombre, e in esperado().items():
            event_id = json.loads((PAYLOADS / nombre).read_text(encoding="utf-8"))["event_id"]
            r = c.get("/leads/estado", params={"event_id": event_id}).json()
            assert (r["estado"], r["ruta"]) == (e["estado"], e["ruta"]), nombre


# --- El script contra un servidor real en loopback -------------------------------------------

def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def servidor(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_ACTIVO", "1")
    puerto = puerto_libre()
    srv = uvicorn.Server(uvicorn.Config(create_app(tmp_path / "leads.db"), host="127.0.0.1", port=puerto,
                                        log_level="warning"))
    hilo = threading.Thread(target=srv.run, daemon=True)
    hilo.start()
    limite = time.monotonic() + 10
    while not srv.started and time.monotonic() < limite:
        time.sleep(0.05)
    assert srv.started
    yield f"http://127.0.0.1:{puerto}"
    srv.should_exit = True
    hilo.join(10)


def test_script_con_servidor_real_coincide(servidor, capsys):
    v = cargar_script()
    assert v.main(["--url", servidor, "--pausa", "0", "--timeout", "20"]) == 0
    out = capsys.readouterr().out
    for nombre, e in esperado().items():
        assert f"{nombre}\t{e['estado']}\t{e['ruta'] or '-'}\tOK" in out
    assert "10/10 coinciden" in out
    assert not any(p in out.lower() for p in DATOS_PERSONALES)
    # Repetirlo contra la misma base da lo mismo (idempotencia por event_id).
    assert v.main(["--url", servidor, "--pausa", "0", "--timeout", "20"]) == 0


def test_script_sale_con_1_si_algo_no_coincide(servidor, tmp_path, capsys):
    otro = json.loads(ESPERADO.read_text(encoding="utf-8"))
    otro["archivos"]["evento_08.json"] = {"estado": "procesado", "ruta": "tibio"}
    ruta = tmp_path / "esperado.json"
    ruta.write_text(json.dumps(otro), encoding="utf-8")
    v = cargar_script()
    assert v.main(["--url", servidor, "--pausa", "0", "--timeout", "20", "--esperado", str(ruta)]) == 1
    out = capsys.readouterr().out
    assert "evento_08.json\tprocesado\tbasura\tNO COINCIDE (esperado procesado/tibio)" in out
    assert "9/10 coinciden" in out


def test_script_sin_servidor_sale_con_1(capsys):
    v = cargar_script()
    url = f"http://127.0.0.1:{puerto_libre()}"  # nadie escucha
    assert v.main(["--url", url, "--pausa", "0", "--timeout", "1"]) == 1
    out = capsys.readouterr().out
    assert "evento_01.json\tsin respuesta" in out


def test_script_rechaza_url_no_http():
    v = cargar_script()
    assert v.main(["--url", "file:///etc/passwd"]) == 2


def test_script_solo_usa_la_biblioteca_estandar():
    import ast
    import sys
    arbol = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    modulos = {a.name.split(".")[0] for n in ast.walk(arbol) if isinstance(n, ast.Import) for a in n.names}
    modulos |= {n.module.split(".")[0] for n in ast.walk(arbol) if isinstance(n, ast.ImportFrom) and n.module}
    assert modulos <= set(sys.stdlib_module_names), modulos - set(sys.stdlib_module_names)
