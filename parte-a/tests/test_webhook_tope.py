"""Paso 0a (D16): tope de tamaño del cuerpo del webhook."""
import json
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import create_app

URL = "/webhook/leads"
TOPE = 4096


@pytest.fixture
def ruta_db(tmp_path):
    return tmp_path / "leads.db"


@pytest.fixture
def cliente(ruta_db, monkeypatch):
    monkeypatch.setenv("WEBHOOK_MAX_BYTES", str(TOPE))
    with TestClient(create_app(ruta_db)) as c:
        yield c


def cuerpo_de(tamano: int) -> bytes:
    """JSON válido de exactamente 'tamano' bytes (relleno con espacios)."""
    base = json.dumps({"event_id": "evt_tope", "event_type": "lead.created", "lead": {}}).encode()
    assert len(base) <= tamano
    return base + b" " * (tamano - len(base))


def contar(ruta_db, tabla="eventos"):
    with sqlite3.connect(ruta_db) as c:
        return c.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


def test_justo_en_el_tope_se_acepta(cliente, ruta_db):
    r = cliente.post(URL, content=cuerpo_de(TOPE))
    assert r.status_code == 202
    assert contar(ruta_db) == 1


def test_tope_mas_uno_da_413_sin_persistir(cliente, ruta_db, caplog):
    cuerpo = cuerpo_de(TOPE + 1)
    r = cliente.post(URL, content=cuerpo)
    assert r.status_code == 413
    assert "evt_tope" not in r.text and len(r.content) < 200  # no repite el cuerpo
    assert contar(ruta_db) == 0 and contar(ruta_db, "trabajos") == 0
    assert str(TOPE + 1) in caplog.text and "evt_tope" not in caplog.text


def _trozos(cuerpo: bytes, tam=1000):
    for i in range(0, len(cuerpo), tam):
        yield cuerpo[i:i + tam]


def test_chunked_sin_content_length(cliente, ruta_db):
    r = cliente.post(URL, content=_trozos(cuerpo_de(TOPE)))
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 202
    r = cliente.post(URL, content=_trozos(cuerpo_de(TOPE * 3)))
    assert r.status_code == 413
    assert contar(ruta_db) == 1


def test_content_length_declarado_menor_que_el_real(cliente, ruta_db):
    r = cliente.post(URL, content=_trozos(cuerpo_de(TOPE * 2)), headers={"Content-Length": "100"})
    assert r.status_code == 413
    assert contar(ruta_db) == 0


def test_content_length_mayor_que_el_tope_no_lee_el_cuerpo(cliente, ruta_db):
    leidos = []

    def trozos():
        for t in _trozos(cuerpo_de(TOPE * 4)):
            leidos.append(len(t))
            yield t

    r = cliente.post(URL, content=trozos(), headers={"Content-Length": str(TOPE * 4)})
    assert r.status_code == 413
    assert contar(ruta_db) == 0


# "١٢٣" (dígitos árabes) va como bytes crudos: httpx no envía texto no ASCII en cabeceras.
@pytest.mark.parametrize("valor", ["abc", "-5", "+5", "1.5", "", " ", "١٢٣".encode(), "9" * 5000])
def test_content_length_invalido(cliente, ruta_db, valor):
    r = cliente.post(URL, content=b"{}", headers={"Content-Length": valor})
    assert r.status_code in (400, 413)
    if valor != "9" * 5000:
        assert r.status_code == 400
    assert contar(ruta_db) == 0


def test_contrato_existente_sin_cambios(cliente, ruta_db):
    cuerpo = json.dumps({"event_id": "evt_c", "event_type": "lead.created", "lead": {}}).encode()
    assert cliente.post(URL, content=cuerpo).status_code == 202
    assert cliente.post(URL, content=cuerpo).status_code == 200
    assert cliente.post(URL, content=b"no es json").status_code == 202


@pytest.mark.parametrize("valor", ["4095", "4194305", "abc", "0"])
def test_webhook_max_bytes_fuera_de_rango_no_arranca(tmp_path, monkeypatch, valor):
    monkeypatch.setenv("WEBHOOK_MAX_BYTES", valor)
    with pytest.raises(config.ConfigError, match="WEBHOOK_MAX_BYTES"):
        create_app(tmp_path / "leads.db")


def test_webhook_max_bytes_por_defecto():
    assert config.cargar_config({}).webhook_max_bytes == 262144
