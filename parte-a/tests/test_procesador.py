"""Bloque 2a: normalización, validación y duplicados de negocio."""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.normalizar import (
    clave_nombre, normalizar_email, normalizar_nombre, normalizar_telefono, normalizar_texto,
)
from app.procesador import procesar_evento

PAYLOADS = Path(__file__).resolve().parents[2] / "payloads"
URL = "/webhook/leads"


@pytest.fixture
def ruta_db(tmp_path) -> Path:
    return tmp_path / "leads.db"


@pytest.fixture
def cliente(ruta_db):
    with TestClient(create_app(ruta_db)) as c:
        yield c


@pytest.fixture
def con(ruta_db, cliente):
    c = db.conectar(ruta_db)
    yield c
    c.close()


def enviar(cliente, cuerpo) -> int:
    """Envía y devuelve el id interno (eventos.id) del evento."""
    if not isinstance(cuerpo, bytes):
        cuerpo = json.dumps(cuerpo).encode()
    r = cliente.post(URL, content=cuerpo)
    assert r.status_code in (200, 202)
    return _id_por_recepcion(cliente.app.state.db_path, r.json()["recepcion_id"])


def _id_por_recepcion(ruta, recepcion_id) -> int:
    with sqlite3.connect(ruta) as c:
        return c.execute("SELECT id FROM eventos WHERE recepcion_id = ?", (recepcion_id,)).fetchone()[0]


def evento(con, evento_id) -> sqlite3.Row:
    return con.execute("SELECT * FROM eventos WHERE id = ?", (evento_id,)).fetchone()


def contar_leads(con) -> int:
    return con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]


_AUTO = object()


def lead_valido(nombre="Ana Pérez", phone="0991112233", email="ana@example.com",
                event_id=_AUTO, **extra):
    """Cuerpo válido; por defecto con un event_id único. event_id=None lo omite."""
    lead = {"full_name": nombre, "phone": phone, "email": email, "message": "Hola"}
    lead.update(extra)
    cuerpo = {"event_type": "lead.created",
              "source": {"platform": "instagram", "campaign": "Sonrisa Blanca"},
              "lead": lead}
    if event_id is _AUTO:
        event_id = f"evt_test_{uuid.uuid4().hex}"
    if event_id is not None:
        cuerpo["event_id"] = event_id
    return cuerpo


# --- Funciones puras ---------------------------------------------------------

@pytest.mark.parametrize("valor, esperado", [
    ("0991234567", "+593991234567"),
    ("098 765 4321", "+593987654321"),
    ("+593 99 123 4567", "+593991234567"),
    (593987650021, "+593987650021"),
    ("0987 111 222", "+593987111222"),
    ("099.123.4567", "+593991234567"),
    ("+1 (305) 555-0142", "+13055550142"),
    ("1111111111", None),
    (None, None),
    ("abc", None),
    ("", None),
    (True, None),
    (["0991234567"], None),
])
def test_normalizar_telefono(valor, esperado):
    assert normalizar_telefono(valor) == esperado


@pytest.mark.parametrize("valor, esperado", [
    (" Carla.Mendoza@example.com ", "carla.mendoza@example.com"),
    ("juan+test@sub.example.com", "juan+test@sub.example.com"),
    ("promo@ganadinero.example", "promo@ganadinero.example"),
    ("ana@example.xn--p1ai", "ana@example.xn--p1ai"),
    ("no-es-correo", None),
    (12345, None),
    (None, None),
    ("a\u0000@example.com", None),
    ("a@b..com", None),
    ("a@b", None),
    ("a@@b.com", None),
    (".a@b.com", None),
    ("a.@b.com", None),
    ("a..b@c.com", None),
    ("a b@c.com", None),
    ("a@-b.com", None),
    ("a@b-.com", None),
    ("ñ@example.com", None),
    ("K@example.com", None),  # signo Kelvin: .lower() lo volvería ASCII
    ("a@example.c0m", None),
    ("a@example.c", None),
    ("a@b@example.com", None),
    ("x" * 65 + "@example.com", None),
    ("x@" + "a" * 64 + ".com", None),
    ("x@" + "a" * 250 + ".com", None),
])
def test_normalizar_email(valor, esperado):
    assert normalizar_email(valor) == esperado


def test_nombre_y_clave():
    assert normalizar_nombre("  carla   mendoza ") == "carla mendoza"
    assert normalizar_nombre(None) == ""
    assert clave_nombre("Carla  Mendoza") == clave_nombre("carla mendoza") == "carla mendoza"
    assert clave_nombre("João Pedro Almeida") == "joao pedro almeida"


def test_normalizar_texto():
    assert normalizar_texto("  Quito ") == "Quito"
    assert normalizar_texto(["Quito"]) == "Quito"
    assert normalizar_texto(["Quito", "Cumbayá"]) == "Quito, Cumbayá"
    assert normalizar_texto(123) is None
    assert normalizar_texto([1, 2]) is None


# --- Los 10 payloads -----------------------------------------------------------

def test_los_diez_payloads_en_orden(cliente, con):
    ids = {}
    for f in sorted(PAYLOADS.glob("evento_*.json")):
        ids[f.stem[-2:]] = enviar(cliente, f.read_bytes())
    for evento_id in sorted(set(ids.values())):
        procesar_evento(con, evento_id)

    def ev(n):
        return evento(con, ids[n])

    assert ids["01"] == ids["02"]
    assert con.execute("SELECT COUNT(*) FROM eventos").fetchone()[0] == 9

    assert ev("01")["estado"] == "pendiente"
    assert ev("01")["telefono_e164"] == "+593991234567"
    assert ev("01")["lead_id"] is not None

    assert ev("03")["estado"] == "duplicado"
    assert ev("03")["duplicado_de"] == "evt_01JB7Q3M2X4K8R9T5W6Y"
    assert ev("03")["lead_id"] == ev("01")["lead_id"]
    assert ev("03")["motivo"].startswith("Mismo contacto y nombre que un evento anterior (")
    assert "carla" not in ev("03")["motivo"].lower()
    r = cliente.get("/leads/estado", params={"event_id": ev("03")["duplicado_de"]})
    assert r.status_code == 200 and r.json()["lead_id"] == ev("01")["lead_id"]
    [m03] = con.execute("SELECT * FROM lead_mensajes").fetchall()
    assert m03["evento_id"] == ids["03"] and m03["lead_id"] == ev("01")["lead_id"]
    assert m03["servicio"] == "Combo limpieza + blanqueamiento"

    assert ev("04")["estado"] == "pendiente" and ev("04")["telefono_e164"] == "+13055550142"

    assert ev("05")["telefono_e164"] is None
    assert ev("05")["email"] == "lectora.anonima@example.com"
    assert ev("05")["estado"] == "pendiente"

    assert ev("06")["telefono_e164"] == "+593987650021"
    ciudad06 = con.execute("SELECT ciudad FROM leads WHERE evento_id = ?", (ids["06"],)).fetchone()[0]
    assert ciudad06 == "Quito"

    assert ev("07")["telefono_e164"] == "+593987654321"

    assert ev("08")["estado"] == "pendiente"
    assert ev("08")["telefono_e164"] is None
    assert ev("08")["email"] == "promo@ganadinero.example"

    for n in ("01", "04", "05", "06", "07", "08", "09", "10"):
        assert ev(n)["estado"] == "pendiente", n
        assert ev(n)["lead_id"] is not None, n
    assert contar_leads(con) == 8

    adv05 = json.loads(con.execute(
        "SELECT advertencias FROM leads WHERE evento_id = ?", (ids["05"],)).fetchone()[0])
    assert "telefono_ausente" in adv05 and "nombre_vacio" in adv05
    adv08 = json.loads(con.execute(
        "SELECT advertencias FROM leads WHERE evento_id = ?", (ids["08"],)).fetchone()[0])
    assert "telefono_invalido" in adv08


# --- Rechazos -----------------------------------------------------------------

def test_sin_contactos_validos_rechazado(cliente, con):
    i = enviar(cliente, lead_valido(phone="1111111111", email=None))
    procesar_evento(con, i)
    e = evento(con, i)
    assert e["estado"] == "rechazado"
    assert e["motivo"] == "Sin teléfono ni correo válidos: teléfono inválido, correo ausente"
    assert "1111111111" not in e["motivo"]
    assert contar_leads(con) == 0


def test_sin_telefono_y_correo_mal_formado_rechazado(cliente, con):
    i = enviar(cliente, lead_valido(phone=None, email="a@b..com"))
    procesar_evento(con, i)
    e = evento(con, i)
    assert e["estado"] == "rechazado"
    assert e["motivo"] == "Sin teléfono ni correo válidos: teléfono ausente, correo inválido"
    assert contar_leads(con) == 0


def test_event_type_distinto_rechazado(cliente, con):
    cuerpo = lead_valido()
    cuerpo["event_type"] = "lead.deleted"
    i = enviar(cliente, cuerpo)
    procesar_evento(con, i)
    assert evento(con, i)["estado"] == "rechazado"
    assert evento(con, i)["motivo"] == "Tipo de evento no soportado"


def test_event_type_ausente_rechazado(cliente, con):
    cuerpo = lead_valido()
    del cuerpo["event_type"]
    i = enviar(cliente, cuerpo)
    procesar_evento(con, i)
    assert evento(con, i)["motivo"] == "Tipo de evento no soportado"


@pytest.mark.parametrize("lead", [None, "texto", ["x"]])
def test_lead_ausente_o_no_objeto_rechazado(cliente, con, lead):
    cuerpo = {"event_type": "lead.created"}
    if lead is not None:
        cuerpo["lead"] = lead
    i = enviar(cliente, cuerpo)
    procesar_evento(con, i)
    assert evento(con, i)["estado"] == "rechazado"
    assert evento(con, i)["motivo"] == "El evento no trae datos del lead"


def test_nombre_y_mensaje_vacios_no_rechazan(cliente, con):
    cuerpo = lead_valido(nombre="")
    del cuerpo["lead"]["message"]
    i = enviar(cliente, cuerpo)
    procesar_evento(con, i)
    assert evento(con, i)["estado"] == "pendiente"
    adv = json.loads(con.execute("SELECT advertencias FROM leads").fetchone()[0])
    assert {"nombre_vacio", "mensaje_vacio"} <= set(adv)


def test_valores_desconocidos_solo_advierten(cliente, con):
    cuerpo = lead_valido(service_interest="Implantes")
    cuerpo["source"] = {"platform": "tiktok", "campaign": "Nueva"}
    i = enviar(cliente, cuerpo)
    procesar_evento(con, i)
    assert evento(con, i)["estado"] == "pendiente"
    fila = con.execute("SELECT servicio, plataforma, advertencias FROM leads").fetchone()
    assert fila["servicio"] == "Implantes" and fila["plataforma"] == "tiktok"
    assert {"servicio_desconocido", "plataforma_desconocida", "campana_desconocida"} <= set(
        json.loads(fila["advertencias"]))


# --- Duplicados de negocio -------------------------------------------------------

def test_mismo_telefono_distinto_nombre_no_es_duplicado(cliente, con):
    a = enviar(cliente, lead_valido(nombre="Ana Pérez", email=None))
    b = enviar(cliente, lead_valido(nombre="Luis Pérez", email=None))
    procesar_evento(con, a)
    procesar_evento(con, b)
    assert evento(con, b)["estado"] == "pendiente"
    assert evento(con, a)["lead_id"] != evento(con, b)["lead_id"]
    assert contar_leads(con) == 2


def test_mismo_correo_mismo_nombre_es_duplicado(cliente, con):
    a = enviar(cliente, lead_valido(nombre="Ana Pérez", phone=None))
    b = enviar(cliente, lead_valido(nombre="ana perez", phone="0987654321"))
    procesar_evento(con, a)
    procesar_evento(con, b)
    assert evento(con, b)["estado"] == "duplicado"
    assert evento(con, b)["duplicado_de"] == evento(con, a)["event_id"]
    assert contar_leads(con) == 1
    # duplicado_de siempre se puede consultar por event_id.
    r = cliente.get("/leads/estado", params={"event_id": evento(con, b)["duplicado_de"]})
    assert r.status_code == 200
    assert r.json()["lead_id"] == evento(con, b)["lead_id"]


def test_original_sin_event_id_no_genera_duplicado(cliente, con):
    a = enviar(cliente, lead_valido(event_id=None))
    b = enviar(cliente, lead_valido(event_id="evt_con_id"))
    procesar_evento(con, a)
    procesar_evento(con, b)
    assert evento(con, a)["event_id"] is None
    assert evento(con, b)["estado"] == "pendiente"
    assert evento(con, b)["duplicado_de"] is None
    assert evento(con, a)["lead_id"] != evento(con, b)["lead_id"]
    assert contar_leads(con) == 2
    assert con.execute("SELECT COUNT(*) FROM eventos WHERE estado = 'duplicado'").fetchone()[0] == 0


def test_duplicado_conserva_su_mensaje_en_el_lead_original(cliente, con):
    a = enviar(cliente, lead_valido(message="Solo quiero precios", service_interest="Limpieza"))
    b = enviar(cliente, lead_valido(
        message="Tengo sangrado abundante y quiero atención inmediata", service_interest="Valoración"))
    procesar_evento(con, a)
    procesar_evento(con, b)

    eb = evento(con, b)
    assert eb["estado"] == "duplicado"
    assert eb["motivo"].startswith("Mismo contacto y nombre que un evento anterior (")
    assert contar_leads(con) == 1

    [m] = con.execute("SELECT * FROM lead_mensajes").fetchall()
    assert m["lead_id"] == evento(con, a)["lead_id"]
    assert m["evento_id"] == b
    assert m["mensaje"] == "Tengo sangrado abundante y quiero atención inmediata"
    assert m["servicio"] == "Valoración"
    assert m["recibido_at"] == eb["received_at"]

    # Reprocesar no duplica el mensaje ni cambia el estado público.
    assert procesar_evento(con, b)["estado"] == "duplicado"
    assert con.execute("SELECT COUNT(*) FROM lead_mensajes").fetchone()[0] == 1


def test_nombres_vacios_no_son_duplicado(cliente, con):
    a = enviar(cliente, lead_valido(nombre=""))
    b = enviar(cliente, lead_valido(nombre=""))
    procesar_evento(con, a)
    procesar_evento(con, b)
    assert evento(con, b)["estado"] == "pendiente"
    assert contar_leads(con) == 2


def _envejecer(con, evento_id, dias):
    antes = (datetime.now(timezone.utc) - timedelta(days=dias)).strftime(db.FORMATO_UTC)
    con.execute("UPDATE eventos SET received_at = ? WHERE id = ?", (antes, evento_id))


def test_duplicado_fuera_de_la_ventana_no_es_duplicado(cliente, con):
    a = enviar(cliente, lead_valido())
    procesar_evento(con, a)
    _envejecer(con, a, 8)
    b = enviar(cliente, lead_valido())
    procesar_evento(con, b)
    assert evento(con, b)["estado"] == "pendiente"
    assert contar_leads(con) == 2


def test_duplicado_dentro_de_la_ventana(cliente, con):
    a = enviar(cliente, lead_valido())
    procesar_evento(con, a)
    _envejecer(con, a, 6)
    b = enviar(cliente, lead_valido())
    procesar_evento(con, b)
    assert evento(con, b)["estado"] == "duplicado"


def test_ventana_configurable(cliente, con, monkeypatch):
    monkeypatch.setenv("DUPLICADO_VENTANA_DIAS", "2")
    a = enviar(cliente, lead_valido())
    procesar_evento(con, a)
    _envejecer(con, a, 3)
    b = enviar(cliente, lead_valido())
    procesar_evento(con, b)
    assert evento(con, b)["estado"] == "pendiente"


def test_original_rechazado_no_cuenta(cliente, con):
    a = enviar(cliente, lead_valido())
    procesar_evento(con, a)
    con.execute("UPDATE eventos SET estado = 'rechazado', motivo = 'x' WHERE id = ?", (a,))
    b = enviar(cliente, lead_valido())
    procesar_evento(con, b)
    assert evento(con, b)["estado"] == "pendiente"


def test_toma_el_original_mas_antiguo(cliente, con):
    a = enviar(cliente, lead_valido(email=None))
    procesar_evento(con, a)
    # b comparte nombre pero no contacto con a: es otro lead.
    b = enviar(cliente, lead_valido(phone=None, email="ana@example.com"))
    procesar_evento(con, b)
    c = enviar(cliente, lead_valido())  # coincide con a (teléfono) y con b (correo)
    procesar_evento(con, c)
    assert evento(con, c)["lead_id"] == evento(con, a)["lead_id"]


# --- Idempotencia y API ----------------------------------------------------------

def test_procesar_dos_veces_una_sola_fila(cliente, con):
    i = enviar(cliente, lead_valido())
    primero = procesar_evento(con, i)
    segundo = procesar_evento(con, i)
    assert primero == segundo
    assert contar_leads(con) == 1


def test_estado_incluye_recepcion_id(cliente, con):
    i = enviar(cliente, (PAYLOADS / "evento_01.json").read_bytes())
    procesar_evento(con, i)
    r = cliente.get("/leads/estado", params={"event_id": "evt_01JB7Q3M2X4K8R9T5W6Y"})
    esperado = set(db.CAMPOS_CONTRATO) | {"recepcion_id"}
    assert set(r.json()) == esperado
    assert r.json()["telefono_e164"] == "+593991234567"
    lista = cliente.get("/leads/estado", params={"desde": "2000-01-01T00:00:00Z"}).json()
    assert set(lista[0]) == esperado
    assert lista[0]["recepcion_id"] == evento(con, i)["recepcion_id"]
