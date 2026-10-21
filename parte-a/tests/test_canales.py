"""Paso 2 (5b): canales de aviso telegram, n8n, archivo y compuesto. Sin red: MockTransport."""
import json
import logging

import httpx
import pytest

from app import config
from app.canales import CanalCompuesto, CanalN8n, CanalTelegram, ErrorCanal, crear_canal
from app.main import create_app

TOKEN = "123456789:AAH-token_secreto_de_prueba_xyz"
CHAT = "-1001234567890"
N8N_URL = "https://n8n.example/webhook/abc-secreto-en-la-ruta"
SECRETO = "s3cr3to-webhook-n8n"
AVISO = {"id": 7, "evento_id": 42, "tipo": "lead_caliente", "creado_at": "2026-11-02T13:00:00.000000Z"}


class Servidor:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.peticiones: list[httpx.Request] = []

    def __call__(self, request):
        self.peticiones.append(request)
        r = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(r, Exception):
            raise r
        return r


def telegram(respuestas):
    s = Servidor(respuestas)
    return CanalTelegram(TOKEN, CHAT, transport=httpx.MockTransport(s)), s


def n8n(respuestas, secreto=SECRETO):
    s = Servidor(respuestas)
    return CanalN8n(N8N_URL, secreto, transport=httpx.MockTransport(s)), s


OK_TG = httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


# --- Telegram ---------------------------------------------------------------------------------------

def test_telegram_forma_exacta_de_la_peticion():
    canal, s = telegram([OK_TG])
    canal.enviar("Hola *mundo* [x](y)", AVISO)
    [p] = s.peticiones
    assert p.method == "POST" and p.url.host == "api.telegram.org"
    assert p.url.path == f"/bot{TOKEN}/sendMessage"
    cuerpo = json.loads(p.content)
    assert cuerpo == {"chat_id": int(CHAT), "text": "Hola *mundo* [x](y)", "disable_web_page_preview": True}
    assert "parse_mode" not in cuerpo


def test_telegram_chat_por_nombre_de_canal():
    s = Servidor([OK_TG])
    CanalTelegram(TOKEN, "@canal_recepcion", transport=httpx.MockTransport(s)).enviar("x", AVISO)
    assert json.loads(s.peticiones[0].content)["chat_id"] == "@canal_recepcion"


def test_telegram_recorta_a_4000():
    canal, s = telegram([OK_TG])
    canal.enviar("x" * 5000, AVISO)
    texto = json.loads(s.peticiones[0].content)["text"]
    assert len(texto) == 4000 and texto.endswith("…")


@pytest.mark.parametrize("respuesta, tipo", [
    (httpx.Response(200, json={"ok": False}), "permanente"),
    (httpx.Response(200, content=b"no es json"), "temporal"),
    (httpx.Response(400, json={"ok": False}), "permanente"),
    (httpx.Response(401), "permanente"),
    (httpx.Response(403), "permanente"),
    (httpx.Response(404), "permanente"),
    (httpx.Response(408), "temporal"),
    (httpx.Response(429, headers={"Retry-After": "5"}), "temporal"),
    (httpx.Response(500), "temporal"),
    (httpx.Response(502), "temporal"),
    (httpx.Response(503), "temporal"),
    (httpx.Response(302, headers={"Location": "https://otro.example/"}), "permanente"),
    (httpx.ReadTimeout("t"), "temporal"),
    (httpx.ConnectTimeout("t"), "temporal"),
    (httpx.ConnectError("c"), "temporal"),
    (httpx.Response(200, content=b'{"ok": true, "x": "' + b"a" * (70 * 1024) + b'"}'), "temporal"),
])
def test_telegram_tabla_de_estados(respuesta, tipo):
    canal, s = telegram([respuesta])
    with pytest.raises(ErrorCanal) as e:
        canal.enviar("hola", AVISO)
    assert e.value.tipo == tipo
    assert len(s.peticiones) == 1  # sin reintentos internos ni redirecciones seguidas


def test_telegram_exito():
    canal, s = telegram([OK_TG])
    assert canal.enviar("hola", AVISO) is None


# --- n8n -----------------------------------------------------------------------------------------------

def test_n8n_forma_exacta_y_secreto():
    canal, s = n8n([httpx.Response(200)])
    canal.enviar("Texto del aviso", AVISO)
    [p] = s.peticiones
    assert p.method == "POST" and str(p.url) == N8N_URL
    assert p.headers["X-Webhook-Secret"] == SECRETO
    assert p.headers["content-type"].startswith("application/json")
    assert json.loads(p.content) == {"tipo": "lead_caliente", "evento_id": 42, "texto": "Texto del aviso",
                                     "creado_at": "2026-11-02T13:00:00.000000Z"}


def test_n8n_sin_secreto_no_envia_cabecera():
    canal, s = n8n([httpx.Response(204)], secreto=None)
    canal.enviar("x", AVISO)
    assert "x-webhook-secret" not in {k.lower() for k in s.peticiones[0].headers}


@pytest.mark.parametrize("estado", [200, 201, 202, 204])
def test_n8n_2xx_es_exito(estado):
    canal, _ = n8n([httpx.Response(estado)])
    canal.enviar("x", AVISO)


@pytest.mark.parametrize("respuesta, tipo", [
    (httpx.Response(404), "temporal"),       # flujo inactivo: recuperable
    (httpx.Response(408), "temporal"),
    (httpx.Response(429), "temporal"),
    (httpx.Response(500), "temporal"),
    (httpx.Response(503), "temporal"),
    (httpx.Response(400), "permanente"),
    (httpx.Response(401), "permanente"),
    (httpx.Response(403), "permanente"),
    (httpx.Response(422), "permanente"),
    (httpx.Response(301, headers={"Location": "https://otro.example/"}), "permanente"),
    (httpx.ReadTimeout("t"), "temporal"),
    (httpx.ConnectError("c"), "temporal"),
])
def test_n8n_tabla_de_estados(respuesta, tipo):
    canal, s = n8n([respuesta])
    with pytest.raises(ErrorCanal) as e:
        canal.enviar("x", AVISO)
    assert e.value.tipo == tipo and len(s.peticiones) == 1


def test_n8n_lee_como_maximo_64_kib():
    leidos = []

    def cuerpo():
        for _ in range(1000):  # ~1 MiB en trozos de 1 KiB
            leidos.append(1024)
            yield b"x" * 1024

    canal, _ = n8n([httpx.Response(200, content=cuerpo())])
    canal.enviar("x", AVISO)
    assert sum(leidos) <= 64 * 1024 + 1024


# --- Secretos fuera de logs, excepciones y repr ------------------------------------------------------------

def test_secretos_fuera_de_logs_excepciones_y_repr(caplog):
    caplog.set_level(logging.DEBUG)
    errores = []
    for canal, _ in (telegram([httpx.Response(500)]), telegram([httpx.ConnectError("x")]),
                     n8n([httpx.Response(401)]), n8n([httpx.ReadTimeout("x")])):
        try:
            canal.enviar("x", AVISO)
        except ErrorCanal as e:
            errores.append(e)
        errores.append(repr(canal))
    cfg = config.cargar_config({"AVISOS_CANAL": "telegram,n8n,archivo", "TELEGRAM_BOT_TOKEN": TOKEN,
                                "TELEGRAM_CHAT_ID": CHAT, "N8N_AVISOS_URL": N8N_URL, "N8N_SECRETO": SECRETO})
    compuesto = crear_canal(cfg)
    texto = caplog.text + " ".join(str(e) + repr(e) + str(e.__cause__) + str(e.__context__) for e in errores
                                   if isinstance(e, Exception)) + " ".join(x for x in errores if isinstance(x, str))
    texto += repr(cfg) + repr(compuesto) + str(cfg)
    for secreto in (TOKEN, "token_secreto", N8N_URL, "abc-secreto-en-la-ruta", SECRETO, CHAT):
        assert secreto not in texto, secreto
    assert not [r for r in caplog.records if r.name.startswith(("httpx", "httpcore"))]
    assert all(e.__cause__ is None for e in errores if isinstance(e, ErrorCanal))


# --- Canal compuesto ------------------------------------------------------------------------------------------

class Doble:
    def __init__(self, nombre, error=None):
        self.nombre, self.error, self.llamadas = nombre, error, 0

    def enviar(self, texto, aviso, presupuesto=None):
        self.llamadas += 1
        if self.error:
            raise self.error


def test_compuesto_envia_a_todos_en_orden_y_exito_parcial(caplog):
    caplog.set_level(logging.WARNING)
    a, b, c = Doble("n8n", ErrorCanal("temporal", "http_500")), Doble("telegram"), Doble("archivo")
    CanalCompuesto([a, b, c]).enviar("x", AVISO)
    assert (a.llamadas, b.llamadas, c.llamadas) == (1, 1, 1)
    assert "éxito parcial" in caplog.text and "n8n:temporal" in caplog.text


@pytest.mark.parametrize("errores, tipo", [
    ([ErrorCanal("temporal", "a"), ErrorCanal("temporal", "b")], "temporal"),
    ([ErrorCanal("permanente", "a"), ErrorCanal("permanente", "b")], "permanente"),
    ([ErrorCanal("permanente", "a"), ErrorCanal("temporal", "b")], "temporal"),
    ([ErrorCanal("permanente", "a"), RuntimeError("x")], "temporal"),
])
def test_compuesto_todos_fallan(errores, tipo):
    canales = [Doble(f"c{k}", e) for k, e in enumerate(errores)]
    with pytest.raises(ErrorCanal) as e:
        CanalCompuesto(canales).enviar("x", AVISO)
    assert e.value.tipo == tipo


def test_crear_canal_respeta_el_orden(tmp_path):
    cfg = config.cargar_config({"AVISOS_CANAL": "n8n, archivo", "N8N_AVISOS_URL": N8N_URL,
                                "AVISOS_ARCHIVO": str(tmp_path / "a.jsonl")})
    canal = crear_canal(cfg)
    assert [c.nombre for c in canal.canales] == ["n8n", "archivo"]


# --- Configuración -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("entorno, variable", [
    ({"AVISOS_CANAL": "archivo,archivo"}, "AVISOS_CANAL"),
    ({"AVISOS_CANAL": "archivo,telegram,n8n,archivo"}, "AVISOS_CANAL"),
    ({"AVISOS_CANAL": "smtp"}, "AVISOS_CANAL"),
    ({"AVISOS_CANAL": "telegram"}, "TELEGRAM_BOT_TOKEN"),
    ({"AVISOS_CANAL": "telegram", "TELEGRAM_BOT_TOKEN": TOKEN}, "TELEGRAM_CHAT_ID"),
    ({"AVISOS_CANAL": "telegram", "TELEGRAM_BOT_TOKEN": "con espacio 123456789012", "TELEGRAM_CHAT_ID": CHAT}, "TELEGRAM_BOT_TOKEN"),
    ({"AVISOS_CANAL": "telegram", "TELEGRAM_BOT_TOKEN": "corto", "TELEGRAM_CHAT_ID": CHAT}, "TELEGRAM_BOT_TOKEN"),
    ({"AVISOS_CANAL": "telegram", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "abc"}, "TELEGRAM_CHAT_ID"),
    ({"AVISOS_CANAL": "n8n"}, "N8N_AVISOS_URL"),
    ({"AVISOS_CANAL": "n8n", "N8N_AVISOS_URL": "ftp://n8n.example"}, "N8N_AVISOS_URL"),
    ({"AVISOS_CANAL": "n8n", "N8N_AVISOS_URL": "https://u:clave@n8n.example/x"}, "N8N_AVISOS_URL"),
    ({"AVISOS_CANAL": "n8n", "N8N_AVISOS_URL": N8N_URL, "N8N_SECRETO": "con espacio"}, "N8N_SECRETO"),
    ({"N8N_ERRORES_URL": "no es url"}, "N8N_ERRORES_URL"),
])
def test_config_de_canales_invalida_no_arranca(tmp_path, monkeypatch, entorno, variable):
    for k, v in entorno.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(config.ConfigError, match=variable) as e:
        create_app(tmp_path / "leads.db")
    for valor in entorno.values():
        if valor not in ("archivo", "telegram", "n8n", "smtp") and "," not in valor:
            assert valor not in str(e.value)


def test_aviso_unico_con_los_canales_activos(tmp_path, monkeypatch, caplog):
    for k, v in {"AVISOS_CANAL": "telegram,n8n", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT,
                 "N8N_AVISOS_URL": N8N_URL}.items():
        monkeypatch.setenv(k, v)
    caplog.set_level(logging.INFO)
    create_app(tmp_path / "leads.db")
    mensajes = [r.getMessage() for r in caplog.records if "canales de aviso" in r.getMessage()]
    assert len(mensajes) == 1 and "telegram, n8n" in mensajes[0]
    assert TOKEN not in caplog.text and N8N_URL not in caplog.text
