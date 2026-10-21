"""Bloque 6.3: scripts/prueba_humo_real.py, siempre con dobles (MockTransport). Sin red real.

El script real lo ejecuta el usuario; aquí solo se comprueba qué pide y qué imprime.
"""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prueba_humo_real.py"
URL_API = "https://pacientes.ejemplo-secreto.test/base"
TOKEN = "tok-SECRETO-pacientes-123456"
CLAVE = "sk-CLAVE-SECRETA-llm-987654"
PACIENTE = {"id": "pac_777", "nombre_completo": "Carla Mendoza Ruiz", "telefono": "+593990000000",
            "email": "carla@example.com", "sede_habitual": "norte", "ultima_visita": "2026-01-10",
            "estado": "activo"}
VALORES_PROHIBIDOS = ("SECRETO", "secreta", "ejemplo-secreto", "pac_777", "carla", "mendoza", "+593",
                      "0990000000", "2026-01-10", "anthropic.com", "blanqueamiento")


def cargar():
    spec = importlib.util.spec_from_file_location("prueba_humo_real", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("API_PACIENTES_URL", URL_API)
    monkeypatch.setenv("API_PACIENTES_TOKEN", TOKEN)


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "modelo-de-prueba")
    monkeypatch.setenv("LLM_API_KEY", CLAVE)


class Servidor:
    """Doble de la API de pacientes: registra cada petición."""
    def __init__(self, pacientes, codigo=200):
        self.pacientes, self.codigo, self.peticiones = pacientes, codigo, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.peticiones.append(request)
        if self.codigo != 200:
            return httpx.Response(self.codigo, json={"error": "detalle con TOKEN " + TOKEN})
        if request.url.path.endswith("/tratamientos"):
            return httpx.Response(200, json={"data": [{"tipo": "Blanqueamiento", "estado": "pendiente"}],
                                             "next_cursor": None})
        return httpx.Response(200, json={"data": self.pacientes, "next_cursor": None})


def salida_llm(**cambios):
    base = {"intencion": "agendar", "urgencia": "media", "presupuesto_estimado_usd": None,
            "siguiente_accion": "agendar_cita", "ruta_sugerida": "caliente", "confianza": 0.9,
            "motivo": "Pide agendar"}
    base.update(cambios)
    return json.dumps(base)


class LLMDoble:
    def __init__(self, texto=None, codigo=200):
        self.texto, self.codigo, self.peticiones = texto or salida_llm(), codigo, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.peticiones.append(request)
        if self.codigo != 200:
            return httpx.Response(self.codigo, json={"error": {"message": "fallo"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": self.texto}],
                                         "stop_reason": "end_turn"})


def sin_valores(texto: str):
    bajo = texto.lower()
    for v in VALORES_PROHIBIDOS:
        assert v.lower() not in bajo, v


# --- Sin configuración ---------------------------------------------------------------------------

def test_sin_configuracion_no_hace_peticiones(capsys):
    servidor, doble = Servidor([PACIENTE]), LLMDoble()
    m = cargar()
    assert m.main(["--llm"], transporte_pacientes=httpx.MockTransport(servidor),
                  transporte_llm=httpx.MockTransport(doble)) == 0
    out = capsys.readouterr().out
    assert "API de pacientes: no configurada" in out and "LLM: LLM_PROVIDER=none" in out
    assert servidor.peticiones == [] and doble.peticiones == []


# --- API de pacientes ----------------------------------------------------------------------------

def test_un_paciente_dos_peticiones_y_solo_claves(api, capsys):
    servidor = Servidor([PACIENTE])
    assert cargar().main([], transporte_pacientes=httpx.MockTransport(servidor)) == 0
    out = capsys.readouterr().out
    assert len(servidor.peticiones) == 2
    assert servidor.peticiones[0].url.params["telefono"] == "+593990000000"  # teléfono ficticio
    assert servidor.peticiones[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert servidor.peticiones[1].url.path.endswith("/pac_777/tratamientos")
    assert "búsqueda: HTTP 200" in out and "tratamientos: HTTP 200" in out
    assert "claves: data, next_cursor" in out
    assert "claves de cada elemento: email, estado, id, nombre_completo, sede_habitual, telefono, ultima_visita" in out
    assert "elementos: 1" in out
    sin_valores(out)


@pytest.mark.parametrize("pacientes", [[], [PACIENTE, {**PACIENTE, "id": "pac_778"}]])
def test_sin_paciente_unico_no_pide_tratamientos(api, capsys, pacientes):
    servidor = Servidor(pacientes)
    assert cargar().main([], transporte_pacientes=httpx.MockTransport(servidor)) == 0
    out = capsys.readouterr().out
    assert len(servidor.peticiones) == 1
    assert "tratamientos: se omite" in out
    sin_valores(out)


@pytest.mark.parametrize("codigo", [401, 500])
def test_error_de_la_api_solo_codigo(api, capsys, codigo):
    servidor = Servidor([PACIENTE], codigo=codigo)
    assert cargar().main([], transporte_pacientes=httpx.MockTransport(servidor)) == 1
    out = capsys.readouterr().out
    assert f"búsqueda: HTTP {codigo}" in out
    assert len(servidor.peticiones) == 1  # sin reintentos: máximo 3 peticiones siempre
    sin_valores(out)


def test_nunca_mas_de_tres_peticiones_a_pacientes(api):
    m = cargar()
    contador = m.Contador(httpx.MockTransport(Servidor([PACIENTE])), maximo=m.MAX_PETICIONES_PACIENTES)
    assert m.MAX_PETICIONES_PACIENTES == 3
    for _ in range(3):
        contador.handle_request(httpx.Request("GET", "https://x.test/"))
    with pytest.raises(m.LimiteDePeticiones):
        contador.handle_request(httpx.Request("GET", "https://x.test/"))


# --- LLM -----------------------------------------------------------------------------------------

def test_llm_una_peticion_validador_acepta_y_ruta(llm, capsys):
    doble = LLMDoble()
    assert cargar().main(["--llm"], transporte_llm=httpx.MockTransport(doble)) == 0
    out = capsys.readouterr().out
    assert len(doble.peticiones) == 1
    enviado = doble.peticiones[0].content.decode()
    assert "0990000000" not in enviado and "[NUMERO]" in enviado  # mensaje ficticio enmascarado
    assert "LLM: HTTP 200" in out and "validador: aceptada" in out and "ruta: caliente" in out
    assert "claves de la salida: confianza, intencion, motivo" in out
    sin_valores(out)


def test_llm_salida_invalida_validador_rechaza(llm, capsys):
    doble = LLMDoble(texto="esto no es json")
    assert cargar().main(["--llm"], transporte_llm=httpx.MockTransport(doble)) == 1
    out = capsys.readouterr().out
    assert len(doble.peticiones) == 1  # sin el reintento de ClienteLLM.clasificar
    assert "validador: rechazada (" in out and "ruta: tibio (solo reglas)" in out
    sin_valores(out)


@pytest.mark.parametrize("codigo", [401, 500, 429])
def test_llm_error_una_sola_peticion(llm, capsys, codigo):
    doble = LLMDoble(codigo=codigo)
    assert cargar().main(["--llm"], transporte_llm=httpx.MockTransport(doble)) == 1
    out = capsys.readouterr().out
    assert len(doble.peticiones) == 1 and f"LLM: HTTP {codigo}" in out
    sin_valores(out)


def test_sin_flag_llm_no_llama_al_llm(llm, capsys):
    doble = LLMDoble()
    assert cargar().main([], transporte_llm=httpx.MockTransport(doble)) == 0
    assert doble.peticiones == [] and "LLM: se omite (sin --llm)" in capsys.readouterr().out


def test_configuracion_invalida_no_muestra_valores(monkeypatch, capsys):
    monkeypatch.setenv("API_PACIENTES_URL", "ftp://usuario:" + TOKEN + "@host")
    assert cargar().main([]) == 2
    captura = capsys.readouterr()
    sin_valores(captura.out + captura.err)
