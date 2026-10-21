"""Paso 5 (5c, D21 y D22): /leads/resumen y la CLI, con construir_resumen como única fuente."""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cli, db, despachador, errores
from app.main import create_app
from app.resumen import construir_resumen

AHORA = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
DESDE = AHORA - timedelta(hours=24)
PAYLOADS = Path(__file__).resolve().parents[2] / "payloads"


def t(momento):
    return momento.strftime(db.FORMATO_UTC)


@pytest.fixture
def ruta(tmp_path):
    r = tmp_path / "leads.db"
    db.inicializar(r)
    return r


@pytest.fixture
def con(ruta):
    c = db.conectar(ruta)
    c.execute("PRAGMA foreign_keys = OFF")  # las filas se fabrican a mano en estos tests
    yield c
    c.close()


def evento(con, recibido, estado="pendiente", ruta_lead=None):
    r = db.registrar_recepcion(con, cuerpo=b"{}", event_id=None, created_at_origen=None, estado="pendiente",
                               motivo=None)
    i = con.execute("SELECT id FROM eventos WHERE recepcion_id = ?", (r.recepcion_id,)).fetchone()[0]
    con.execute("UPDATE eventos SET received_at = ?, estado = ?, ruta = ?, motivo = ? WHERE id = ?",
                (t(recibido), estado, ruta_lead, None if estado in ("pendiente", "procesado") and ruta_lead != "basura"
                 else "motivo", i))
    return i


def aviso(con, evento_id, *, estado="pendiente", creado, vence, enviado=None, latencia=None, tipo="lead_caliente"):
    con.execute("""INSERT INTO avisos (evento_id, tipo, canal, estado, intentos, proximo_intento, vence_at,
                                       creado_at, enviado_at, latencia_s)
                   VALUES (?, ?, 'archivo', ?, 1, ?, ?, ?, ?, ?)""",
                (evento_id, tipo, estado, t(creado), t(vence), t(creado), t(enviado) if enviado else None, latencia))


def resumen(con, **kw):
    return construir_resumen(con, AHORA, DESDE, pendiente_antiguo_s=900, **kw)


# --- Forma exacta con un caso por cada estado y ruta -----------------------------------------------

def test_forma_exacta_con_todos_los_estados_y_rutas(con):
    hace1h = AHORA - timedelta(hours=1)
    cal = evento(con, hace1h, "procesado", "caliente")
    cal2 = evento(con, hace1h, "procesado", "caliente")
    tib = evento(con, hace1h, "procesado", "tibio")
    evento(con, hace1h, "procesado", "basura")
    evento(con, hace1h, "duplicado")
    evento(con, hace1h, "rechazado")
    evento(con, hace1h, "fallido")
    evento(con, AHORA - timedelta(seconds=2000), "pendiente")
    evento(con, AHORA - timedelta(hours=30), "procesado", "tibio")  # fuera de la ventana
    aviso(con, cal, estado="enviado", creado=hace1h, vence=hace1h + timedelta(seconds=300),
          enviado=hace1h + timedelta(seconds=12), latencia=12.0)
    aviso(con, cal2, estado="fallido", creado=hace1h, vence=hace1h + timedelta(seconds=300))
    con.execute("INSERT INTO seguimientos VALUES (?, 'lead', 'Hola', 'pendiente', ?)", (tib, t(hace1h)))
    errores.configurar(con.execute("PRAGMA database_list").fetchone()[2], 900)
    errores.reportar_error("llm", "timeout")
    errores.desconfigurar()

    r = resumen(con, hilos={"trabajador": True, "despachador": True})
    assert r == {
        "generado_at": "2026-11-02T15:00:00.000000Z",
        "desde": "2026-11-01T15:00:00.000000Z",
        "salud": "degradado",
        "eventos": {"total": 8, "por_estado": {"procesado": 4, "duplicado": 1, "rechazado": 1, "fallido": 1,
                                                "pendiente": 1}},
        "rutas": {"caliente": 2, "tibio": 1, "basura": 1},
        "pendiente_mas_antiguo_s": 2000,
        "avisos": {"pendientes": 0, "enviados": 1, "fallidos": 1, "vencidos": 1,
                   "latencia_s": {"p50": 12.0, "max": 12.0}, "en_plazo": {"cumplen": 1, "total": 2}},
        "seguimientos_pendientes": 1,
        "incidencias_abiertas": 1,
        "hilos": {"trabajador": True, "despachador": True},
        "alertas": ["pendiente_antiguo", "aviso_vencido", "aviso_fallido", "eventos_fallidos",
                    "incidencias_pendientes"],
    }


def test_base_vacia(con):
    r = resumen(con, hilos={"trabajador": True, "despachador": True})
    assert r["salud"] == "ok" and r["alertas"] == []
    assert r["eventos"] == {"total": 0, "por_estado": dict.fromkeys(
        ("procesado", "duplicado", "rechazado", "fallido", "pendiente"), 0)}
    assert r["rutas"] == {"caliente": 0, "tibio": 0, "basura": 0} and r["pendiente_mas_antiguo_s"] is None
    assert r["avisos"] == {"pendientes": 0, "enviados": 0, "fallidos": 0, "vencidos": 0,
                           "latencia_s": {"p50": None, "max": None}, "en_plazo": {"cumplen": 0, "total": 0}}


def test_latencia_p50_y_max(con):
    for k, lat in enumerate((5.0, 10.0, 30.0)):
        i = evento(con, AHORA - timedelta(hours=1), "procesado", "caliente")
        creado = AHORA - timedelta(hours=1)
        aviso(con, i, estado="enviado", creado=creado, vence=creado + timedelta(seconds=20),
              enviado=creado + timedelta(seconds=lat), latencia=lat)
    a = resumen(con, hilos=None)["avisos"]
    assert a["latencia_s"] == {"p50": 10.0, "max": 30.0}
    assert a["en_plazo"] == {"cumplen": 2, "total": 3}


# --- Cada alerta por separado --------------------------------------------------------------------------

def test_alerta_pendiente_antiguo(con):
    evento(con, AHORA - timedelta(seconds=1000))
    assert resumen(con)["alertas"] == ["pendiente_antiguo"]
    con.execute("DELETE FROM trabajos")
    con.execute("DELETE FROM eventos")
    evento(con, AHORA - timedelta(seconds=800))
    assert resumen(con)["alertas"] == []


def test_alerta_aviso_vencido_de_cualquier_fecha(con):
    viejo = AHORA - timedelta(days=10)
    i = evento(con, viejo, "procesado", "caliente")
    aviso(con, i, creado=viejo, vence=viejo + timedelta(seconds=300))
    r = resumen(con)
    assert r["alertas"] == ["aviso_vencido"] and r["avisos"]["vencidos"] == 1 and r["avisos"]["pendientes"] == 0


def test_alerta_aviso_fallido(con):
    i = evento(con, AHORA - timedelta(minutes=1), "procesado", "caliente")
    aviso(con, i, estado="fallido", creado=AHORA - timedelta(minutes=1), vence=AHORA + timedelta(minutes=4))
    assert resumen(con)["alertas"] == ["aviso_fallido"]


def test_alerta_eventos_fallidos(con):
    evento(con, AHORA - timedelta(hours=1), "fallido")
    assert resumen(con)["alertas"] == ["eventos_fallidos"]


def test_alerta_incidencias_pendientes(con, ruta):
    errores.configurar(ruta, 900)
    errores.reportar_error("api_pacientes", "credenciales")
    errores.desconfigurar()
    r = resumen(con)
    assert r["alertas"] == ["incidencias_pendientes"] and r["incidencias_abiertas"] == 1


def test_alerta_hilo_caido(con):
    r = resumen(con, hilos={"trabajador": True, "despachador": False})
    assert r["alertas"] == ["hilo_caido"] and r["salud"] == "degradado"
    assert resumen(con, hilos={"trabajador": None, "despachador": None})["alertas"] == []


# --- Endpoint ----------------------------------------------------------------------------------------------

def test_endpoint_por_defecto_ultimas_24_h(tmp_path):
    with TestClient(create_app(tmp_path / "leads.db")) as c:
        r = c.get("/leads/resumen")
    assert r.status_code == 200
    cuerpo = r.json()
    desde = datetime.strptime(cuerpo["desde"], db.FORMATO_UTC).replace(tzinfo=timezone.utc)
    generado = datetime.strptime(cuerpo["generado_at"], db.FORMATO_UTC).replace(tzinfo=timezone.utc)
    assert abs((generado - desde) - timedelta(hours=24)) < timedelta(seconds=5)
    assert cuerpo["hilos"] == {"trabajador": None, "despachador": None}  # WORKER_ACTIVO=0
    assert cuerpo["salud"] == "ok"


@pytest.mark.parametrize("desde, codigo", [
    ("2026-11-01T10:00:00-05:00", 200),
    ("2026-11-01T15:00:00Z", 200),
    ("2026-11-01T15:00:00+00:00", 200),
    ("2026-11-01T15:00:00", 400),          # sin zona horaria
    ("ayer", 400),
    ("2026-13-01T00:00:00Z", 400),
    ("0001-01-01T00:00:00+01:00", 400),
    ("<script>alert(1)</script>", 400),
])
def test_endpoint_desde(tmp_path, desde, codigo):
    with TestClient(create_app(tmp_path / "leads.db")) as c:
        r = c.get("/leads/resumen", params={"desde": desde})
    assert r.status_code == codigo
    if codigo == 200:
        assert r.json()["desde"] == "2026-11-01T15:00:00.000000Z"
    else:
        assert desde not in r.text and "script" not in r.text


def test_endpoint_con_hilos_reales(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_ACTIVO", "1")
    with TestClient(create_app(tmp_path / "leads.db")) as c:
        assert c.get("/leads/resumen").json()["hilos"] == {"trabajador": True, "despachador": True}
        c.app.state.despachador.detener(timeout=5)
        r = c.get("/leads/resumen").json()
    assert r["hilos"]["despachador"] is False and "hilo_caido" in r["alertas"]


def test_endpoint_sin_datos_personales_y_contratos_intactos(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_ACTIVO", "1")
    with TestClient(create_app(tmp_path / "leads.db")) as c:
        for f in sorted(PAYLOADS.glob("evento_*.json")):
            c.post("/webhook/leads", content=f.read_bytes())
        limite = time.monotonic() + 10
        while time.monotonic() < limite and c.get("/leads/resumen").json()["avisos"]["enviados"] < 2:
            time.sleep(0.05)
        r = c.get("/leads/resumen")
        texto = r.text
        assert set(c.get("/leads/estado", params={"event_id": "evt_01JB7Q3M2X4K8R9T5W6Y"}).json()) == {
            "event_id", "estado", "ruta", "lead_id", "duplicado_de", "telefono_e164", "email", "motivo", "recepcion_id"}
        assert c.get("/salud").json() == {"status": "ok"}
    for dato in json.loads("[" + ",".join(f.read_text() for f in sorted(PAYLOADS.glob("evento_*.json"))) + "]"):
        lead = dato["lead"]
        for valor in (lead.get("full_name"), lead.get("email"), str(lead.get("phone") or "")):
            if valor and len(valor) > 3:
                assert valor not in texto, valor
    assert "+593" not in texto and "@" not in texto
    cuerpo = r.json()
    assert cuerpo["rutas"] == {"caliente": 2, "tibio": 5, "basura": 1}
    assert cuerpo["eventos"]["por_estado"]["duplicado"] == 1 and cuerpo["avisos"]["enviados"] == 2
    assert cuerpo["seguimientos_pendientes"] == 5


# --- CLI ------------------------------------------------------------------------------------------------------

def _poblar(ruta):
    c = db.conectar(ruta)
    c.execute("PRAGMA foreign_keys = OFF")  # filas fabricadas a mano
    try:
        i = evento(c, datetime.now(timezone.utc) - timedelta(hours=1), "procesado", "caliente")
        creado = datetime.now(timezone.utc) - timedelta(hours=1)
        aviso(c, i, estado="fallido", creado=creado, vence=creado + timedelta(seconds=300))
        j = evento(c, datetime.now(timezone.utc) - timedelta(hours=1), "procesado", "tibio")
        c.execute("INSERT INTO seguimientos VALUES (?, 'lead', 'Hola Carla, gracias por escribir.', 'pendiente', ?)",
                  (j, t(creado)))
    finally:
        c.close()


def test_cli_resumen_json_igual_que_el_endpoint(tmp_path, monkeypatch, capsys):
    ruta = tmp_path / "leads.db"
    with TestClient(create_app(ruta)) as c:
        _poblar(ruta)
        endpoint = c.get("/leads/resumen").json()
    assert cli.main(["--db", str(ruta), "resumen", "--json"]) == 0
    salida = json.loads(capsys.readouterr().out)
    for k in ("generado_at", "desde", "hilos"):
        endpoint.pop(k), salida.pop(k)
    assert salida == endpoint


def test_cli_resumen_texto_y_solo_lectura(tmp_path, capsys):
    ruta = tmp_path / "leads.db"
    db.inicializar(ruta)
    _poblar(ruta)
    antes = ruta.read_bytes()
    assert cli.main(["--db", str(ruta), "resumen"]) == 0
    out = capsys.readouterr().out
    assert "Salud: degradado" in out and "Rutas: caliente 1, tibio 1, basura 0" in out
    assert "Carla" not in out
    assert ruta.read_bytes() == antes


def test_cli_seguimientos_con_advertencia(tmp_path, capsys):
    ruta = tmp_path / "leads.db"
    db.inicializar(ruta)
    _poblar(ruta)
    assert cli.main(["--db", str(ruta), "seguimientos", "--limite", "5"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ADVERTENCIA") and "datos personales" in out
    assert "Hola Carla, gracias por escribir." in out


def test_cli_reintentar_avisos(tmp_path, capsys):
    ruta = tmp_path / "leads.db"
    db.inicializar(ruta)
    _poblar(ruta)
    assert cli.main(["--db", str(ruta), "reintentar-avisos"]) == 0
    assert "1" in capsys.readouterr().out
    c = sqlite3.connect(ruta)
    [(estado, intentos)] = c.execute("SELECT estado, intentos FROM avisos").fetchall()
    c.close()
    assert (estado, intentos) == ("pendiente", 0)
    assert cli.main(["--db", str(ruta), "reintentar-avisos"]) == 0
    assert "0" in capsys.readouterr().out


def test_cli_desde_invalido(tmp_path, capsys):
    ruta = tmp_path / "leads.db"
    db.inicializar(ruta)
    assert cli.main(["--db", str(ruta), "resumen", "--desde", "ayer"]) == 2
    assert "ayer" not in capsys.readouterr().err


def test_cli_base_inexistente(tmp_path, capsys):
    assert cli.main(["--db", str(tmp_path / "no.db"), "resumen"]) == 2
    assert not (tmp_path / "no.db").exists()
