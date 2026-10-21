"""Acceso a SQLite: esquema, conexión y registro atómico de recepciones."""
import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ESTADOS = ("procesado", "duplicado", "rechazado", "fallido", "pendiente")

# Los 8 campos públicos del contrato de estado, en este orden.
CAMPOS_CONTRATO = (
    "event_id", "estado", "ruta", "lead_id",
    "duplicado_de", "telefono_e164", "email", "motivo",
)

FORMATO_UTC = "%Y-%m-%dT%H:%M:%S.%fZ"
BUSY_TIMEOUT_MS = 5000

_ESTADOS_SQL = ", ".join(f"'{e}'" for e in ESTADOS)

ESQUEMA = f"""
CREATE TABLE IF NOT EXISTS eventos (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    recepcion_id      TEXT NOT NULL UNIQUE,
    event_id          TEXT UNIQUE,
    received_at       TEXT NOT NULL CHECK (received_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    created_at_origen TEXT,
    cuerpo_crudo      BLOB NOT NULL,
    hash_cuerpo       TEXT NOT NULL,
    estado            TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ({_ESTADOS_SQL})),
    ruta              TEXT,
    lead_id           TEXT,
    duplicado_de      TEXT,
    telefono_e164     TEXT,
    email             TEXT,
    motivo            TEXT,
    CHECK ((estado = 'procesado' AND ruta IS NOT NULL)
        OR (estado <> 'procesado' AND ruta IS NULL)),
    CHECK (estado NOT IN ('duplicado', 'rechazado', 'fallido')
        OR (motivo IS NOT NULL AND motivo <> ''))
);
CREATE INDEX IF NOT EXISTS idx_eventos_received_at ON eventos(received_at);

CREATE TABLE IF NOT EXISTS trabajos (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    evento_id          INTEGER NOT NULL REFERENCES eventos(id),
    tipo               TEXT NOT NULL DEFAULT 'procesar_evento' CHECK (tipo IN ('procesar_evento')),
    estado             TEXT NOT NULL DEFAULT 'pendiente'
        CHECK (estado IN ('pendiente', 'en_curso', 'hecho', 'fallido')),
    etapa              TEXT NOT NULL DEFAULT 'pendiente',  -- última etapa completada
    intentos           INTEGER NOT NULL DEFAULT 0 CHECK (intentos >= 0),
    max_intentos       INTEGER NOT NULL DEFAULT 5 CHECK (max_intentos >= 1),
    proximo_intento_at TEXT,
    lease_hasta        TEXT,
    reprogramaciones   INTEGER NOT NULL DEFAULT 0 CHECK (reprogramaciones >= 0),
    ultimo_error       TEXT,
    UNIQUE (evento_id, tipo)
);
CREATE INDEX IF NOT EXISTS idx_trabajos_cola ON trabajos(estado, proximo_intento_at);

CREATE TABLE IF NOT EXISTS repeticiones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL REFERENCES eventos(event_id),
    received_at     TEXT NOT NULL,
    hash_cuerpo     TEXT NOT NULL,
    cuerpo_crudo    BLOB NOT NULL,
    mismo_contenido INTEGER NOT NULL CHECK (mismo_contenido IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_repeticiones_event_id ON repeticiones(event_id);

CREATE TABLE IF NOT EXISTS leads (
    lead_id       TEXT PRIMARY KEY,
    evento_id     INTEGER NOT NULL UNIQUE REFERENCES eventos(id),
    nombre        TEXT NOT NULL DEFAULT '',
    clave_nombre  TEXT NOT NULL DEFAULT '',
    telefono_e164 TEXT,
    email         TEXT,
    ciudad        TEXT,
    servicio      TEXT,
    mensaje       TEXT,
    plataforma    TEXT,
    campana       TEXT,
    form_id       TEXT,
    advertencias  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(advertencias)),
    creado_at     TEXT NOT NULL,
    CHECK (telefono_e164 IS NOT NULL OR email IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_leads_telefono ON leads(telefono_e164);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_clave_nombre ON leads(clave_nombre);

-- Mensajes de eventos marcados como duplicado, ligados al lead original.
CREATE TABLE IF NOT EXISTS lead_mensajes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     TEXT NOT NULL REFERENCES leads(lead_id),
    evento_id   INTEGER NOT NULL UNIQUE REFERENCES eventos(id),
    recibido_at TEXT NOT NULL,
    servicio    TEXT,
    mensaje     TEXT,
    creado_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lead_mensajes_lead ON lead_mensajes(lead_id);

-- Resultado de consultar la API de pacientes: una fila por evento (UPSERT). Sin nombre,
-- teléfono, correo ni fecha de nacimiento del paciente (minimización de datos).
CREATE TABLE IF NOT EXISTS enriquecimientos (
    evento_id               INTEGER PRIMARY KEY REFERENCES eventos(id),
    lead_id                 TEXT NOT NULL REFERENCES leads(lead_id),
    estado_enriquecimiento  TEXT NOT NULL CHECK (estado_enriquecimiento IN
        ('paciente', 'no_paciente', 'ambigua', 'no_disponible', 'no_configurado')),
    coincidencia            TEXT CHECK (coincidencia IS NULL
        OR coincidencia IN ('unica', 'ninguna', 'ambigua')),
    paciente_id             TEXT,
    sede_habitual           TEXT,
    ultima_visita           TEXT,
    estado_paciente         TEXT,
    tratamientos_pendientes INTEGER CHECK (tratamientos_pendientes IS NULL OR tratamientos_pendientes >= 0),
    -- 1: se leyeron todas las páginas; 0: tope alcanzado (el conteo es un mínimo); NULL: sin dato
    tratamientos_completos  INTEGER CHECK (tratamientos_completos IS NULL OR tratamientos_completos IN (0, 1)),
    tipos_pendientes        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tipos_pendientes)),
    advertencias            TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(advertencias)),
    motivo                  TEXT,
    consultado_at           TEXT NOT NULL,
    CHECK ((estado_enriquecimiento = 'paciente') = (paciente_id IS NOT NULL)),
    CHECK (estado_enriquecimiento NOT IN ('no_disponible', 'no_configurado', 'ambigua')
        OR (motivo IS NOT NULL AND motivo <> ''))
);

-- Clasificación del lead (una fila por evento, UPSERT). Sin el mensaje (ni enmascarado) ni la
-- respuesta cruda del LLM: solo el resultado validado y, si falló, el tipo de fallo.
CREATE TABLE IF NOT EXISTS clasificaciones (
    evento_id                INTEGER PRIMARY KEY REFERENCES eventos(id),
    lead_id                  TEXT NOT NULL REFERENCES leads(lead_id),
    ruta                     TEXT NOT NULL CHECK (ruta IN ('caliente', 'tibio', 'basura')),
    fuente                   TEXT NOT NULL CHECK (fuente IN ('llm', 'reglas')),
    senales                  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(senales)),
    intencion                TEXT,
    urgencia                 TEXT,
    presupuesto_estimado_usd REAL CHECK (presupuesto_estimado_usd IS NULL
        OR presupuesto_estimado_usd BETWEEN 0 AND 20000),
    siguiente_accion         TEXT NOT NULL,
    confianza                REAL CHECK (confianza IS NULL OR confianza BETWEEN 0 AND 1),
    motivo                   TEXT NOT NULL CHECK (motivo <> ''),
    prompt_version           TEXT,
    prompt_hash              TEXT,
    proveedor                TEXT,
    modelo                   TEXT,
    fallo_llm                TEXT,
    clasificado_at           TEXT NOT NULL,
    CHECK (fuente = 'reglas' OR (prompt_version IS NOT NULL AND prompt_hash IS NOT NULL))
);

-- Bandeja de salida de avisos (outbox). No guarda el texto: se renderiza al enviar desde la
-- base. lease_hasta reserva el aviso mientras se envía (si el proceso muere, otro lo retoma).
CREATE TABLE IF NOT EXISTS avisos (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    evento_id          INTEGER NOT NULL REFERENCES eventos(id),
    tipo               TEXT NOT NULL CHECK (tipo IN ('lead_caliente', 'novedad_duplicado')),
    canal              TEXT NOT NULL,
    estado             TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'enviado', 'fallido')),
    intentos           INTEGER NOT NULL DEFAULT 0 CHECK (intentos >= 0),
    proximo_intento    TEXT NOT NULL,
    lease_hasta        TEXT,
    vence_at           TEXT NOT NULL,
    vencido_notificado INTEGER NOT NULL DEFAULT 0 CHECK (vencido_notificado IN (0, 1)),
    creado_at          TEXT NOT NULL,
    enviado_at         TEXT,
    latencia_s         REAL,
    ultimo_error       TEXT,  -- solo el tipo de error
    UNIQUE (evento_id, tipo),
    CHECK ((estado = 'enviado') = (enviado_at IS NOT NULL)),
    CHECK (estado = 'enviado' OR latencia_s IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_avisos_cola ON avisos(estado, proximo_intento);

-- Bandeja de incidencias del manejador central de errores (D19). Solo códigos, contadores e
-- ids de evento: nunca texto libre. Una sola pendiente por (codigo, clave).
CREATE TABLE IF NOT EXISTS incidencias (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo          TEXT NOT NULL CHECK (length(codigo) BETWEEN 1 AND 40),
    clave           TEXT NOT NULL DEFAULT '' CHECK (length(clave) <= 40),
    detalle         TEXT NOT NULL DEFAULT '' CHECK (length(detalle) <= 200),
    ocurrencias     INTEGER NOT NULL DEFAULT 1 CHECK (ocurrencias >= 1),
    creado_at       TEXT NOT NULL,
    ultima_at       TEXT NOT NULL,
    estado          TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'enviado', 'fallido')),
    intentos        INTEGER NOT NULL DEFAULT 0 CHECK (intentos >= 0),
    proximo_intento TEXT NOT NULL,
    lease_hasta     TEXT,
    enviado_at      TEXT,
    ultimo_error    TEXT,  -- solo el tipo de error
    CHECK ((estado = 'enviado') = (enviado_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_incidencias_pendiente_unica ON incidencias(codigo, clave)
    WHERE estado = 'pendiente';
CREATE INDEX IF NOT EXISTS idx_incidencias_cola ON incidencias(estado, proximo_intento);

-- Primer mensaje de seguimiento para los tibios (lo envía recepción; Bloque 5b).
CREATE TABLE IF NOT EXISTS seguimientos (
    evento_id INTEGER PRIMARY KEY REFERENCES eventos(id),
    lead_id   TEXT NOT NULL REFERENCES leads(lead_id),
    mensaje   TEXT NOT NULL CHECK (length(mensaje) > 0 AND length(mensaje) < 500),
    estado    TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'enviado', 'descartado')),
    creado_at TEXT NOT NULL
);

-- Citas tentativas de los calientes (reglas de la ficha; no consultan una agenda real).
CREATE TABLE IF NOT EXISTS citas_tentativas (
    evento_id          INTEGER NOT NULL REFERENCES eventos(id),
    orden              INTEGER NOT NULL CHECK (orden IN (1, 2)),
    servicio_propuesto TEXT NOT NULL,
    sede               TEXT NOT NULL CHECK (sede IN ('norte', 'valle')),
    inicio             TEXT NOT NULL,
    fin                TEXT NOT NULL,
    precio_usd         INTEGER CHECK (precio_usd IS NULL OR precio_usd >= 0),
    nota               TEXT NOT NULL,
    PRIMARY KEY (evento_id, orden)
);

-- D4: mensaje nuevo de un duplicado con una señal caliente que el original no tenía. El
-- Bloque 5 lo avisará como "actualización de un lead existente".
CREATE TABLE IF NOT EXISTS novedades_duplicado (
    evento_id    INTEGER PRIMARY KEY REFERENCES eventos(id),
    duplicado_de TEXT NOT NULL,
    lead_id      TEXT NOT NULL,
    senales      TEXT NOT NULL CHECK (json_valid(senales)),
    creada_at    TEXT NOT NULL
);
"""


def ahora_utc() -> str:
    return datetime.now(timezone.utc).strftime(FORMATO_UTC)


def conectar(ruta: Path) -> sqlite3.Connection:
    # isolation_level=None: las transacciones se abren y cierran explícitamente.
    con = sqlite3.connect(ruta, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA synchronous = FULL")
    return con


def inicializar(ruta: Path) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = conectar(ruta)
    try:
        modo = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if modo.lower() != "wal":
            raise RuntimeError(f"SQLite no aceptó journal_mode=WAL (quedó en {modo})")
        con.executescript(ESQUEMA)
    finally:
        con.close()


def campos_contrato(fila: sqlite3.Row) -> dict:
    return {c: fila[c] for c in CAMPOS_CONTRATO}


def campos_estado(fila: sqlite3.Row) -> dict:
    """Los 8 campos del contrato más el extra recepcion_id."""
    return {**campos_contrato(fila), "recepcion_id": fila["recepcion_id"]}


@dataclass
class Recepcion:
    nuevo: bool            # False si el event_id ya existía
    recepcion_id: str      # de la fila original cuando nuevo=False
    estado: dict           # 8 campos del contrato del evento (original si repetido)


def registrar_recepcion(
    con: sqlite3.Connection,
    *,
    cuerpo: bytes,
    event_id: str | None,
    created_at_origen: str | None,
    estado: str,
    motivo: str | None,
    max_intentos: int = 5,
) -> Recepcion:
    """Guarda el evento y su trabajo en una sola transacción.

    La deduplicación la decide la restricción UNIQUE(event_id) mediante
    ON CONFLICT DO NOTHING + rowcount, nunca un SELECT previo.
    """
    hash_cuerpo = hashlib.sha256(cuerpo).hexdigest()
    con.execute("BEGIN IMMEDIATE")
    try:
        # Se toma dentro del bloqueo de escritura para que received_at sea
        # monótono con el orden de inserción (id).
        received_at = ahora_utc()
        cur = con.execute(
            """INSERT INTO eventos (recepcion_id, event_id, received_at, created_at_origen,
                                    cuerpo_crudo, hash_cuerpo, estado, motivo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_id) DO NOTHING""",
            (str(uuid.uuid4()), event_id, received_at, created_at_origen,
             cuerpo, hash_cuerpo, estado, motivo),
        )
        if cur.rowcount == 1:
            if estado == "pendiente":
                con.execute(
                    """INSERT INTO trabajos (evento_id, proximo_intento_at, max_intentos)
                       VALUES (?, ?, ?)""",
                    (cur.lastrowid, received_at, max_intentos),
                )
            fila = con.execute("SELECT * FROM eventos WHERE id = ?", (cur.lastrowid,)).fetchone()
            nuevo = True
        else:
            # Conflicto: el original existe (no se borran eventos). No se toca.
            fila = con.execute("SELECT * FROM eventos WHERE event_id = ?", (event_id,)).fetchone()
            con.execute(
                """INSERT INTO repeticiones (event_id, received_at, hash_cuerpo,
                                             cuerpo_crudo, mismo_contenido)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, received_at, hash_cuerpo, cuerpo,
                 int(hash_cuerpo == fila["hash_cuerpo"])),
            )
            nuevo = False
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return Recepcion(nuevo=nuevo, recepcion_id=fila["recepcion_id"], estado=campos_contrato(fila))


def estado_por_event_id(con: sqlite3.Connection, event_id: str) -> dict | None:
    fila = con.execute("SELECT * FROM eventos WHERE event_id = ?", (event_id,)).fetchone()
    return campos_estado(fila) if fila else None


def estados_desde(con: sqlite3.Connection, desde_utc: str) -> list[dict]:
    filas = con.execute(
        "SELECT * FROM eventos WHERE received_at >= ? ORDER BY id", (desde_utc,)
    ).fetchall()
    return [campos_estado(f) for f in filas]
