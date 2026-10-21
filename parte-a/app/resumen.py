"""Resumen operativo (D21): única fuente de datos de GET /leads/resumen y de la CLI.

Sin datos personales: solo conteos, latencias, códigos y el estado de los hilos.
Ventana: eventos por received_at >= desde; avisos por creado_at >= desde. Las alertas de
avisos vencidos o fallidos y las incidencias abiertas miran cualquier fecha.
"""
import re
import statistics
from datetime import datetime, timedelta, timezone

from app import db

ESTADOS = ("procesado", "duplicado", "rechazado", "fallido", "pendiente")
RUTAS = ("caliente", "tibio", "basura")
ALERTAS = ("pendiente_antiguo", "aviso_vencido", "aviso_fallido", "eventos_fallidos",
           "incidencias_pendientes", "hilo_caido")
VENTANA_POR_DEFECTO = timedelta(hours=24)
_OFFSET_CON_ESPACIO = re.compile(r"T.* (\d{2}(:?\d{2})?)$")  # un '+' sin codificar llega como espacio


def _txt(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime(db.FORMATO_UTC)


def _leer(texto: str) -> datetime:
    return datetime.strptime(texto, db.FORMATO_UTC).replace(tzinfo=timezone.utc)


def parsear_desde(valor: str) -> datetime:
    """ISO 8601 CON zona horaria. ValueError si es inválido, no tiene zona o se sale de rango."""
    texto = valor.strip()
    if _OFFSET_CON_ESPACIO.search(texto):
        texto = texto[: texto.rindex(" ")] + "+" + texto[texto.rindex(" ") + 1:]
    momento = datetime.fromisoformat(texto)
    if momento.tzinfo is None:
        raise ValueError("sin zona horaria")
    try:
        return momento.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError("fuera de rango") from None


def _uno(con, sql: str, *args) -> int:
    return con.execute(sql, args).fetchone()[0] or 0


def construir_resumen(con, ahora: datetime, desde: datetime | None = None, *,
                      pendiente_antiguo_s: int = 900, hilos: dict | None = None) -> dict:
    desde = desde or (ahora - VENTANA_POR_DEFECTO)
    t_ahora, t_desde = _txt(ahora), _txt(desde)

    por_estado = dict.fromkeys(ESTADOS, 0)
    por_estado.update(dict(con.execute(
        "SELECT estado, COUNT(*) FROM eventos WHERE received_at >= ? GROUP BY estado", (t_desde,)).fetchall()))
    rutas = dict.fromkeys(RUTAS, 0)
    rutas.update(dict(con.execute(
        "SELECT ruta, COUNT(*) FROM eventos WHERE received_at >= ? AND ruta IS NOT NULL GROUP BY ruta",
        (t_desde,)).fetchall()))
    mas_antiguo = con.execute("SELECT MIN(received_at) FROM eventos WHERE estado = 'pendiente'").fetchone()[0]
    pendiente_s = int((ahora - _leer(mas_antiguo)).total_seconds()) if mas_antiguo else None

    en_ventana = "FROM avisos WHERE creado_at >= ?"
    latencias = [f[0] for f in con.execute(
        f"SELECT latencia_s {en_ventana} AND estado = 'enviado' AND latencia_s IS NOT NULL", (t_desde,))]
    vencidos = _uno(con, "SELECT COUNT(*) FROM avisos WHERE estado <> 'enviado' AND vence_at < ?", t_ahora)
    fallidos_cualquier_fecha = _uno(con, "SELECT COUNT(*) FROM avisos WHERE estado = 'fallido'")
    avisos = {
        "pendientes": _uno(con, f"SELECT COUNT(*) {en_ventana} AND estado = 'pendiente'", t_desde),
        "enviados": _uno(con, f"SELECT COUNT(*) {en_ventana} AND estado = 'enviado'", t_desde),
        "fallidos": _uno(con, f"SELECT COUNT(*) {en_ventana} AND estado = 'fallido'", t_desde),
        "vencidos": vencidos,
        "latencia_s": {"p50": round(statistics.median(latencias), 1) if latencias else None,
                       "max": round(max(latencias), 1) if latencias else None},
        # Solo avisos con resultado ya decidido: enviados o con el plazo vencido.
        "en_plazo": {
            "cumplen": _uno(con, f"SELECT COUNT(*) {en_ventana} AND estado = 'enviado' AND enviado_at <= vence_at",
                            t_desde),
            "total": _uno(con, f"SELECT COUNT(*) {en_ventana} AND (estado = 'enviado' OR vence_at < ?)",
                          t_desde, t_ahora),
        },
    }
    seguimientos = _uno(con, "SELECT COUNT(*) FROM seguimientos WHERE estado = 'pendiente'")
    incidencias = _uno(con, "SELECT COUNT(*) FROM incidencias WHERE estado IN ('pendiente', 'fallido')")
    hilos = {"trabajador": None, "despachador": None} | {k: v for k, v in (hilos or {}).items()
                                                          if k in ("trabajador", "despachador")}

    activas = {
        "pendiente_antiguo": pendiente_s is not None and pendiente_s > pendiente_antiguo_s,
        "aviso_vencido": vencidos > 0,
        "aviso_fallido": fallidos_cualquier_fecha > 0,
        "eventos_fallidos": por_estado["fallido"] > 0,
        "incidencias_pendientes": incidencias > 0,
        "hilo_caido": any(v is False for v in hilos.values()),
    }
    alertas = [a for a in ALERTAS if activas[a]]
    return {
        "generado_at": t_ahora,
        "desde": t_desde,
        "salud": "ok" if not alertas else "degradado",
        "eventos": {"total": sum(por_estado[e] for e in ESTADOS), "por_estado": {e: por_estado[e] for e in ESTADOS}},
        "rutas": {r: rutas[r] for r in RUTAS},
        "pendiente_mas_antiguo_s": pendiente_s,
        "avisos": avisos,
        "seguimientos_pendientes": seguimientos,
        "incidencias_abiertas": incidencias,
        "hilos": hilos,
        "alertas": alertas,
    }


def texto_resumen(r: dict) -> str:
    """Versión legible para la CLI (mismos números que el JSON)."""
    e, a = r["eventos"], r["avisos"]
    estados = ", ".join(f"{k} {v}" for k, v in e["por_estado"].items())
    lat = a["latencia_s"]
    hilos = ", ".join(f"{k} {'sí' if v else ('no' if v is False else 'no disponible')}" for k, v in r["hilos"].items())
    return "\n".join([
        f"Resumen de leads desde {r['desde']} (generado {r['generado_at']})",
        f"Salud: {r['salud']}" + (f" (alertas: {', '.join(r['alertas'])})" if r["alertas"] else ""),
        f"Eventos: total {e['total']} ({estados})",
        f"Rutas: caliente {r['rutas']['caliente']}, tibio {r['rutas']['tibio']}, basura {r['rutas']['basura']}",
        "Pendiente más antiguo: " + (f"{r['pendiente_mas_antiguo_s']} s" if r["pendiente_mas_antiguo_s"] is not None
                                     else "ninguno"),
        f"Avisos: pendientes {a['pendientes']}, enviados {a['enviados']}, fallidos {a['fallidos']}, "
        f"vencidos {a['vencidos']}; latencia p50 {lat['p50']} s, máx {lat['max']} s; "
        f"en plazo {a['en_plazo']['cumplen']}/{a['en_plazo']['total']}",
        f"Seguimientos pendientes: {r['seguimientos_pendientes']}",
        f"Incidencias abiertas: {r['incidencias_abiertas']}",
        f"Hilos: {hilos}",
    ])
