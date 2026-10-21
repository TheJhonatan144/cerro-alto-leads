"""CLI de operación (D22): python -m app.cli [--db RUTA] <orden>

  resumen [--desde ISO] [--json]   resumen sin datos personales (base en solo lectura)
  seguimientos [--limite N]        mensajes redactados para los tibios (¡contiene datos personales!)
  reintentar-avisos                devuelve a 'pendiente' los avisos fallidos

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
    args = p.parse_args(argv)
    try:
        return {"resumen": _resumen, "seguimientos": _seguimientos,
                "reintentar-avisos": _reintentar_avisos}[args.orden](args)
    except (sqlite3.Error, config.ConfigError) as e:
        print(f"no se pudo abrir o usar la base: {type(e).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
