"""Ejemplos del contrato con n8n, generados desde el propio código (con datos ficticios).

    python -m app.ejemplos_n8n    # reescribe parte-a/n8n/ejemplos/*.json

Un test comprueba que los archivos coinciden con lo que genera este módulo, así el contrato
documentado no puede divergir del código.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import agenda, db, plantillas
from app.canales import cuerpo_n8n_aviso, cuerpo_n8n_error
from app.resumen import construir_resumen

DIR = Path(__file__).resolve().parents[1] / "n8n" / "ejemplos"
AHORA = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
RECIBIDO = "2026-11-02T14:58:10.123456Z"
CREADO = "2026-11-02T14:58:11.000000Z"


def _aviso_caliente() -> dict:
    texto = plantillas.aviso_caliente({
        "nombre": "Ana Ejemplo", "telefono_e164": "+593990000000", "email": "ana@example.com",
        "servicio": "Limpieza", "ruta": "caliente", "motivo": "Caliente por reglas: pide agendar o que lo llamen",
        "estado_enriquecimiento": "no_paciente", "tratamientos_pendientes": None, "event_id": "evt_ejemplo_001",
        "lead_id": "00000000-0000-4000-8000-000000000001", "received_at": RECIBIDO,
        "citas": agenda.proponer_citas("Limpieza", None, AHORA, ())})
    return cuerpo_n8n_aviso({"tipo": "lead_caliente", "evento_id": 101, "creado_at": CREADO}, texto)


def _novedad() -> dict:
    texto = plantillas.novedad_duplicado({
        "nombre": "Ana Ejemplo", "telefono_e164": "+593990000000", "email": "ana@example.com",
        "senales": ["urgencia_clinica"], "event_id": "evt_ejemplo_002", "received_at": RECIBIDO,
        "duplicado_de": "evt_ejemplo_001", "lead_id": "00000000-0000-4000-8000-000000000001"})
    return cuerpo_n8n_aviso({"tipo": "novedad_duplicado", "evento_id": 102, "creado_at": CREADO}, texto)


def _error() -> dict:
    return cuerpo_n8n_error({"codigo": "aviso_vencido", "detalle": "origen=avisos evento=101", "ocurrencias": 1,
                             "creado_at": CREADO})


def _resumen() -> dict:
    """Resumen de una base ficticia pequeña, con el mismo código que /leads/resumen."""
    with tempfile.TemporaryDirectory() as d:
        ruta = Path(d) / "ejemplo.db"
        db.inicializar(ruta)
        con = db.conectar(ruta)
        con.execute("PRAGMA foreign_keys = OFF")
        try:
            hace = AHORA - timedelta(hours=2)
            filas = [("procesado", "caliente"), ("procesado", "tibio"), ("procesado", "tibio"),
                     ("procesado", "basura"), ("duplicado", None), ("rechazado", None)]
            for k, (estado, ruta_lead) in enumerate(filas):
                motivo = None if estado == "procesado" and ruta_lead != "basura" else "motivo"
                con.execute("""INSERT INTO eventos (recepcion_id, received_at, cuerpo_crudo, hash_cuerpo, estado,
                                                    ruta, motivo) VALUES (?, ?, x'7b7d', 'h', ?, ?, ?)""",
                            (f"r{k}", hace.strftime(db.FORMATO_UTC), estado, ruta_lead, motivo))
            con.execute("""INSERT INTO avisos (evento_id, tipo, canal, estado, intentos, proximo_intento, vence_at,
                                               creado_at, enviado_at, latencia_s)
                           VALUES (1, 'lead_caliente', 'n8n,archivo', 'enviado', 1, ?, ?, ?, ?, 4.2)""",
                        (hace.strftime(db.FORMATO_UTC), (hace + timedelta(seconds=300)).strftime(db.FORMATO_UTC),
                         hace.strftime(db.FORMATO_UTC), (hace + timedelta(seconds=4.2)).strftime(db.FORMATO_UTC)))
            for evento_id in (2, 3):
                con.execute("INSERT INTO seguimientos VALUES (?, 'lead', 'Hola', 'pendiente', ?)",
                            (evento_id, hace.strftime(db.FORMATO_UTC)))
            return construir_resumen(con, AHORA, None, pendiente_antiguo_s=900,
                                     hilos={"trabajador": True, "despachador": True})
        finally:
            con.close()


def generar() -> dict:
    return {"aviso_caliente": _aviso_caliente(), "novedad_duplicado": _novedad(), "error": _error(),
            "resumen": _resumen()}


def escribir(directorio: Path = DIR) -> list[Path]:
    directorio.mkdir(parents=True, exist_ok=True)
    rutas = []
    for nombre, datos in generar().items():
        ruta = directorio / f"{nombre}.json"
        ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rutas.append(ruta)
    return rutas


if __name__ == "__main__":
    for r in escribir():
        print(r)
    sys.exit(0)
