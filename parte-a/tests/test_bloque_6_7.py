"""Bloque 6.7: correcciones de la revisión externa (hallazgos 1, 2, 3, 6 y 9)."""
import json
import os
import time
from datetime import timedelta

import pytest

from app import canales, despachador
from app.canales import CanalArchivo, ErrorCanal
from app.despachador import despachar_pendientes
from app.plantillas import aviso_caliente, limpiar_campo, seguimiento_tibio
from app.reglas import evaluar
from tests.test_avisos import (  # noqa: F401  (fixtures y helpers del Bloque 5a)
    FallaSiempre, _un_caliente, avisos, cliente, con, evento, leer, ruta_db,
)
from tests.test_plantillas import datos_aviso

AVISO = {"id": 1, "evento_id": 7, "tipo": "lead_caliente"}


def lineas(ruta):
    return [json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines()]


# --- 1) CanalArchivo sin os.fchmod (Windows) ------------------------------------------------------

def test_archivo_sin_fchmod_escribe_el_aviso(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "fchmod", raising=False)
    ruta = tmp_path / "avisos.jsonl"
    CanalArchivo(ruta).enviar("hola", AVISO)
    assert lineas(ruta)[0]["aviso_id"] == 1 and lineas(ruta)[0]["texto"] == "hola"


# --- 2) CanalArchivo: escritura completa en bucle --------------------------------------------------

def test_archivo_escritura_parcial_completa_la_linea(tmp_path, monkeypatch):
    escribir, llamadas = os.write, []

    def un_byte(fd, datos):
        llamadas.append(len(datos))
        return escribir(fd, bytes(datos[:1]))

    monkeypatch.setattr(canales.os, "write", un_byte)
    ruta = tmp_path / "avisos.jsonl"
    texto = "Lead caliente: Ñandú – línea completa"
    CanalArchivo(ruta).enviar(texto, AVISO)
    monkeypatch.undo()
    assert len(llamadas) > 1
    filas = lineas(ruta)
    assert len(filas) == 1 and filas[0]["texto"] == texto and filas[0]["aviso_id"] == 1
    assert ruta.read_bytes().endswith(b"\n")


def test_archivo_sin_progreso_es_error_temporal(tmp_path, monkeypatch):
    monkeypatch.setattr(canales.os, "write", lambda fd, datos: 0)
    with pytest.raises(ErrorCanal) as e:
        CanalArchivo(tmp_path / "avisos.jsonl").enviar("hola", AVISO)
    assert e.value.tipo == "temporal"


def test_archivo_sin_progreso_no_marca_enviado(cliente, con, ruta_db, tmp_path, monkeypatch):
    i, listo = _un_caliente(cliente, ruta_db, con)
    monkeypatch.setattr(canales.os, "write", lambda fd, datos: 0)
    despachar_pendientes(ruta_db, CanalArchivo(tmp_path / "a.jsonl"), listo)
    a = avisos(con, i)[0]
    assert a["estado"] == "pendiente" and a["enviado_at"] is None


# --- 3) Vencimiento: incidencia y marca en la misma transacción ------------------------------------

def _vencidas(con):
    return con.execute("SELECT * FROM incidencias WHERE codigo = 'aviso_vencido'").fetchall()


def _reabrir(con, i, recibido):
    con.execute("UPDATE avisos SET proximo_intento = ?, lease_hasta = NULL WHERE evento_id = ?",
                (despachador._txt(recibido), i))


@pytest.mark.parametrize("disparador", [
    # Falla la segunda escritura (la incidencia) tras hacer la primera (la marca).
    "CREATE TRIGGER falla BEFORE INSERT ON incidencias WHEN NEW.codigo = 'aviso_vencido' "
    "BEGIN SELECT RAISE(ABORT, 'fallo simulado'); END",
    # Falla la marca vencido_notificado.
    "CREATE TRIGGER falla BEFORE UPDATE OF vencido_notificado ON avisos "
    "BEGIN SELECT RAISE(ABORT, 'fallo simulado'); END",
])
def test_vencido_atomico_y_una_sola_vez(cliente, con, ruta_db, disparador):
    i, _ = _un_caliente(cliente, ruta_db, con)
    recibido = leer(evento(con, i)["received_at"])
    con.execute(disparador)
    despachar_pendientes(ruta_db, FallaSiempre(), recibido + timedelta(seconds=400))
    assert avisos(con, i)[0]["vencido_notificado"] == 0 and _vencidas(con) == []  # nada persistió

    con.execute("DROP TRIGGER falla")
    for segundos in (500, 600, 700):  # la siguiente vuelta lo completa; las demás no duplican
        _reabrir(con, i, recibido)
        despachar_pendientes(ruta_db, FallaSiempre(), recibido + timedelta(seconds=segundos))
    assert avisos(con, i)[0]["vencido_notificado"] == 1
    inc = _vencidas(con)
    assert len(inc) == 1 and inc[0]["ocurrencias"] == 1 and f"evento={i}" in inc[0]["detalle"]


# --- 6) Alcance de la negación en urgencias ---------------------------------------------------------

@pytest.mark.parametrize("mensaje, urgencia", [
    ("No tengo fiebre, solo tengo sangrado abundante", True),
    ("No tengo fiebre, solo sangrado abundante", True),
    ("No tengo fiebre, sólo sangrado abundante", True),
    ("No tengo fiebre, solamente un dolor intenso", True),
    ("No tengo fiebre, únicamente la cara hinchada", True),
    ("No tengo fiebre, además la encía está hinchada", True),
    ("No tengo fiebre, también tengo un absceso", True),
    ("No tengo fiebre y solo tengo sangrado abundante", True),
    ("No tengo fiebre, sangra mucho la encía", True),
    ("No tengo fiebre, noto la cara hinchada", True),
    ("no tengo dolor, ni hinchazón", False),
    ("no tengo fiebre, dolor ni hinchazón", False),
    ("No tengo dolor, solo quiero una limpieza", False),
    ("sin dolor ni molestias", False),
])
def test_negacion_cortada_por_conectores(mensaje, urgencia):
    assert ("urgencia_clinica" in evaluar(mensaje).senales) is urgencia


# --- 9) limpiar_campo: dominios sin protocolo -------------------------------------------------------

@pytest.mark.parametrize("valor", ["Ana evil.com/login", "Ana www.x.org", "Ana bit.ly/abc",
                                   "Ana EVIL.COM/login", "Ana https://evil.com"])
def test_campo_sin_dominios(valor):
    t = limpiar_campo(valor, 80)
    assert t == "Ana [enlace]", t


def test_dominio_en_nombre_y_servicio_del_aviso():
    t = aviso_caliente(datos_aviso(nombre="Ana evil.com/login", servicio="Ana evil.com/login"))
    assert "evil.com" not in t
    assert "Nombre: Ana [enlace]" in t and "Servicio de interés: Ana [enlace]" in t


@pytest.mark.parametrize("nombre", ["Dr. Pérez", "J.R. Smith", "María José Coello", "Ma.Coello",
                                    "Sra. Ana M. Ruiz", "O'Neil-Smith Jr."])
def test_nombres_con_puntos_no_se_alteran(nombre):
    assert limpiar_campo(nombre, 80) == nombre


def test_correo_del_contacto_se_conserva():
    assert "Contacto: carla@example.com" in aviso_caliente(datos_aviso(telefono_e164=None))


@pytest.mark.parametrize("valor", ["Ana x@evil.com/login", "Ana x@www.evil.com", "Ana x@https://evil.com"])
def test_enlace_tras_arroba_si_se_marca(valor):
    assert "evil.com" not in limpiar_campo(valor, 80)


def test_agenda_url_configurada_se_conserva():
    t = seguimiento_tibio("Ana", "Limpieza", [], "https://agenda.example.com/cerro-alto")
    assert "https://agenda.example.com/cerro-alto" in t and "[enlace]" not in t


FAMILIAS_CAMPO = ["a.", "a-", "a.com", "a.com/", "a.com ", "www.", "http://", "a@a.com", ".com",
                  "a.a", "<", "<<<", "@", "a.co", "-.com"]


@pytest.mark.parametrize("patron", FAMILIAS_CAMPO)
def test_rendimiento_limpiar_campo(patron):
    texto = (patron * (20_000 // len(patron) + 1))[:20_000]
    inicio = time.perf_counter()
    limpiar_campo(texto, 200)
    transcurrido = time.perf_counter() - inicio
    assert transcurrido < 1.0, f"{patron!r}: {transcurrido:.2f} s"
