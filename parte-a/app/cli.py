"""CLI de operación (D22): python -m app.cli [--db RUTA] <orden>

  resumen [--desde ISO] [--json]   resumen sin datos personales (base en solo lectura)
  seguimientos [--limite N]        mensajes redactados para los tibios (¡contiene datos personales!)
  reintentar-avisos                devuelve a 'pendiente' los avisos fallidos
  listar-fallidos                  eventos fallidos: event_id, etapa e intentos (sin datos personales)
  reprocesar (--event-id X | --todos) [--dry-run]
                                   devuelve a 'pendiente' los eventos fallidos y su trabajo

No arranca hilos. La base sale de --db o de DB_PATH (y .env).
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from app import config, db
from app.resumen import construir_resumen, parsear_desde, texto_resumen

ADVERTENCIA = ("ADVERTENCIA: esta salida contiene datos personales (nombres de pila). Uso local: no la "
               "compartas ni la guardes en archivos públicos.")


def _conectar(ruta: Path, solo_lectura: bool) -> sqlite3.Connection:
    modo = "ro" if solo_lectura else "rw"  # nunca crea una base nueva por error
    con = sqlite3.connect(Path(ruta).resolve().as_uri() + f"?mode={modo}", uri=True, timeout=5,
                          isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def _ruta(args) -> Path:
    if args.db:
        return Path(args.db)
    config.cargar_env()
    return config.cargar_config().db_path


def _resumen(args) -> int:
    try:
        desde = parsear_desde(args.desde) if args.desde else None
    except ValueError:
        print("desde debe ser una fecha ISO 8601 con zona horaria", file=sys.stderr)
        return 2
    ruta = _ruta(args)  # carga .env si hace falta
    cfg = config.cargar_config()
    con = _conectar(ruta, solo_lectura=True)
    try:
        r = construir_resumen(con, datetime.now(timezone.utc), desde, pendiente_antiguo_s=cfg.pendiente_antiguo_s)
    finally:
        con.close()
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else texto_resumen(r))
    return 0


def _seguimientos(args) -> int:
    con = _conectar(_ruta(args), solo_lectura=True)
    try:
        filas = con.execute("""SELECT evento_id, mensaje FROM seguimientos WHERE estado = 'pendiente'
                               ORDER BY creado_at, evento_id LIMIT ?""", (args.limite,)).fetchall()
    finally:
        con.close()
    print(ADVERTENCIA)
    for f in filas:
        print(f"evento {f['evento_id']}: {f['mensaje']}")
    return 0


def _reintentar_avisos(args) -> int:
    con = _conectar(_ruta(args), solo_lectura=False)
    try:
        ahora = datetime.now(timezone.utc).strftime(db.FORMATO_UTC)
        cur = con.execute("""UPDATE avisos SET estado = 'pendiente', intentos = 0, proximo_intento = ?,
                                               lease_hasta = NULL, ultimo_error = NULL
                             WHERE estado = 'fallido'""", (ahora,))
    finally:
        con.close()
    print(f"Avisos devueltos a pendiente: {cur.rowcount}")
    return 0


# Eventos fallidos con su trabajo (puede faltar). Solo columnas sin datos personales.
_FALLIDOS = """SELECT e.id, e.event_id, t.estado AS trabajo, t.etapa, t.intentos, t.lease_hasta,
                      t.ultimo_error
               FROM eventos e LEFT JOIN trabajos t ON t.evento_id = e.id AND t.tipo = 'procesar_evento'
               WHERE e.estado = 'fallido'"""


def _nombre(f) -> str:
    return f["event_id"] if f["event_id"] is not None else f"(sin event_id, id interno {f['id']})"


def _listar_fallidos(args) -> int:
    con = _conectar(_ruta(args), solo_lectura=True)
    try:
        filas = con.execute(_FALLIDOS + " ORDER BY e.id").fetchall()
    finally:
        con.close()
    if not filas:
        print("No hay eventos fallidos")
        return 0
    print("event_id\tetapa\tintentos\tultimo_error")
    for f in filas:
        print(f"{_nombre(f)}\t{f['etapa'] or '-'}\t{f['intentos'] if f['intentos'] is not None else '-'}"
              f"\t{f['ultimo_error'] or '-'}")
    print(f"Total: {len(filas)}")
    return 0


def _reprocesar(args) -> int:
    """Devuelve a 'pendiente' los eventos fallidos y su trabajo (intentos=0) en UNA transacción.

    El trabajo conserva su etapa: se reanuda tras la última completada (las etapas son
    idempotentes, así que no duplica avisos, seguimientos ni citas). Un trabajo 'en_curso' con
    lease vigente es de un trabajador vivo y no se toca. Repetir la orden no cambia nada.
    """
    ruta = _ruta(args)
    max_intentos = config.cargar_config().trabajo_max_intentos  # solo si falta el trabajo
    con = _conectar(ruta, solo_lectura=args.dry_run)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        if not args.dry_run:
            con.execute("BEGIN IMMEDIATE")  # elección y escritura bajo el mismo bloqueo
        try:
            ahora = datetime.now(timezone.utc).strftime(db.FORMATO_UTC)
            if args.event_id is not None:
                ev = con.execute("SELECT estado FROM eventos WHERE event_id = ?", (args.event_id,)).fetchone()
                if ev is None:
                    print("event_id no encontrado", file=sys.stderr)  # sin repetir el valor
                    return 1
                if ev["estado"] != "fallido":
                    print(f"{args.event_id}: no está fallido (estado: {ev['estado']}); nada que hacer")
                filas = con.execute(_FALLIDOS + " AND e.event_id = ?", (args.event_id,)).fetchall()
            else:
                filas = con.execute(_FALLIDOS + " ORDER BY e.id").fetchall()
            elegibles, en_curso = [], []
            for f in filas:
                activa = f["trabajo"] == "en_curso" and f["lease_hasta"] is not None and f["lease_hasta"] >= ahora
                (en_curso if activa else elegibles).append(f)
            if not args.dry_run:
                for f in elegibles:
                    con.execute(
                        """INSERT INTO trabajos (evento_id, proximo_intento_at, max_intentos) VALUES (?, ?, ?)
                           ON CONFLICT(evento_id, tipo) DO UPDATE SET
                               estado = 'pendiente', intentos = 0, proximo_intento_at = excluded.proximo_intento_at,
                               lease_hasta = NULL, reprogramaciones = 0, ultimo_error = NULL""",
                        (f["id"], ahora, max_intentos))
                    con.execute("UPDATE eventos SET estado = 'pendiente', motivo = NULL "
                                "WHERE id = ? AND estado = 'fallido'", (f["id"],))
                con.execute("COMMIT")
        finally:
            if con.in_transaction:
                con.execute("ROLLBACK")
    finally:
        con.close()
    prefijo = "(simulación: no se cambia nada) " if args.dry_run else ""
    for f in elegibles:
        print(f"{prefijo}{_nombre(f)}\tetapa={f['etapa'] or '-'}\tintentos={f['intentos']}\t-> pendiente")
    for f in en_curso:
        print(f"{_nombre(f)}: omitido, en curso con lease vigente (lo procesa un trabajador vivo)")
    if args.dry_run:
        print(f"simulación: se devolverían a pendiente: {len(elegibles)}")
    else:
        print(f"Eventos devueltos a pendiente: {len(elegibles)}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="Operación del sistema de leads")
    p.add_argument("--db", help="ruta de la base (por defecto, DB_PATH)")
    sub = p.add_subparsers(dest="orden", required=True)
    r = sub.add_parser("resumen", help="resumen sin datos personales")
    r.add_argument("--desde", help="ISO 8601 con zona horaria (por defecto, últimas 24 h)")
    r.add_argument("--json", action="store_true", help="salida JSON (igual que /leads/resumen)")
    s = sub.add_parser("seguimientos", help="mensajes de seguimiento pendientes (datos personales)")
    s.add_argument("--limite", type=int, default=20)
    sub.add_parser("reintentar-avisos", help="devuelve a pendiente los avisos fallidos")
    sub.add_parser("listar-fallidos", help="eventos fallidos (event_id, etapa, intentos)")
    rp = sub.add_parser("reprocesar", help="devuelve a pendiente los eventos fallidos")
    cual = rp.add_mutually_exclusive_group(required=True)
    cual.add_argument("--event-id", help="solo este evento")
    cual.add_argument("--todos", action="store_true", help="todos los eventos fallidos")
    rp.add_argument("--dry-run", action="store_true", help="muestra qué haría sin cambiar nada")
    args = p.parse_args(argv)
    try:
        return {"resumen": _resumen, "seguimientos": _seguimientos,
                "reintentar-avisos": _reintentar_avisos, "listar-fallidos": _listar_fallidos,
                "reprocesar": _reprocesar}[args.orden](args)
    except (sqlite3.Error, config.ConfigError) as e:
        print(f"no se pudo abrir o usar la base: {type(e).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
