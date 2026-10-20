# DECISIONES

Leyenda: **[R]** requisito confirmado · **[D]** decisión propia · **[S]** supuesto · **[L]** limitación conocida.

## Bloque 1 · Recepción, persistencia e idempotencia

### Persistencia
- **[R]** Se persiste antes de responder 2xx. El 202 se devuelve solo después del `COMMIT`; cualquier `sqlite3.Error` (al conectar, insertar o confirmar) responde **503** con `Retry-After: 5`, nunca un ack falso.
- **[R]** SQLite con `sqlite3` estándar, `journal_mode=WAL`, `busy_timeout=5000 ms` y `synchronous=FULL` en cada conexión.
- **[D]** Una conexión por petición, transacción `BEGIN IMMEDIATE` (toma el bloqueo de escritura al inicio, así las escrituras concurrentes se serializan y esperan vía `busy_timeout` en vez de fallar a mitad).
- **[D]** `received_at` se toma *dentro* de la transacción, así es monótono con el `id` (orden de llegada real = orden de commit). Formato fijo `YYYY-MM-DDTHH:MM:SS.ffffffZ`, validado con `CHECK ... GLOB`; por ser de ancho fijo, comparar texto equivale a comparar fechas.
- **[D]** `cuerpo_crudo` es `BLOB`: guarda los bytes exactos, incluso si no son UTF-8 válido. `hash_cuerpo` = SHA-256 de esos bytes.
- **[D]** `DB_PATH` relativo se resuelve contra `parte-a/`, no contra el directorio actual (por defecto `parte-a/data/leads.db`, ignorado por git). Las variables se leen del entorno; no se carga `.env` automáticamente.
- **[D]** El trabajo `procesar_evento` se crea en la misma transacción que el evento, **solo para eventos `pendiente`**. Un evento `rechazado` en la recepción no genera trabajo (no hay nada que procesar). `UNIQUE(evento_id, tipo)` garantiza un único trabajo por evento.

### Idempotencia
- **[R]** `INSERT ... ON CONFLICT(event_id) DO NOTHING` + `rowcount`. No hay SELECT previo para decidir; el SELECT del original solo ocurre *después* de que la restricción `UNIQUE` reportó el conflicto, dentro de la misma transacción.
- **[R]** Repetido: no se sobrescribe nada del original; se inserta una fila en `repeticiones` y se responde **200** con `recepcion_id` y los 8 campos del original.
- **[D]** `mismo_contenido` compara el hash de los **bytes crudos**: el mismo JSON con distinto espaciado u orden de claves cuenta como contenido distinto (`0`). Es conservador; si importa, el Bloque 2 puede comparar el JSON canónico.
- **[D]** `event_id`: si es texto se recortan los espacios de los extremos (`" evt_x "` y `"evt_x"` son el mismo evento); si es entero (no booleano) se convierte a texto (`12345` → `"12345"`). Si falta, queda vacío tras el recorte o es de otro tipo (booleano, decimal, lista, objeto, `null`) → `NULL`, y el evento se guarda igual como `pendiente` (sin deduplicación posible). `GET /leads/estado?event_id=` aplica el mismo recorte.
- **[S]** `created_at_origen` = `created_at` del payload si es texto no vacío, sin validar ni normalizar.

### Cuerpos inválidos
- **[R]** Cuerpo que no es JSON (incluye cuerpo vacío o bytes que no son UTF-8 válido) → `rechazado`, motivo "El cuerpo no es JSON válido". JSON que no es objeto (lista, número, texto) → `rechazado`, motivo "El JSON recibido no es un objeto". Ambos responden **202**, nunca 500: el error es del contenido, reintentar no lo arregla.
- **[D]** Aunque una lista contenga objetos con `event_id`, no se extrae: queda `event_id NULL`.

### Esquema (CHECK)
- `estado ∈ {procesado, duplicado, rechazado, fallido, pendiente}`, por defecto `pendiente`.
- `ruta` no nula **si y solo si** `estado = 'procesado'`.
- `motivo` obligatorio (no vacío) en `duplicado`, `rechazado` y `fallido`. **[L]** El "motivo obligatorio en ruta basura" no se fuerza todavía: los valores de `ruta` se definen en el Bloque 2.
- **[D]** `trabajos.estado` aún sin CHECK: sus estados se fijan cuando exista el procesador.

### GET /leads/estado
- **[R]** Los 8 campos del contrato (confirmados por el enunciado) son: `event_id, estado, ruta, lead_id, duplicado_de, telefono_e164, email, motivo`.
- **[S]** `?desde=` filtra por `received_at` (momento en que **nosotros** recibimos el evento), **no** por `created_at` del origen, que no está validado y puede venir mal o en otra zona.
- **[D]** `desde` acepta ISO 8601 con `Z` o desplazamiento; sin zona = UTC; se convierte a UTC. Un `+` sin codificar en la URL llega como espacio (`...T10:00:00 05:00`) y se interpreta como `+05:00`. Formato inválido → 400.
- **[D]** Orden de llegada = `ORDER BY id`. Respuesta = arreglo JSON de objetos con los 8 campos.
- **[D]** `event_id` y `desde` a la vez → 400 (son consultas distintas). Ninguno → 400. `event_id` vacío → 400. Desconocido → 404.
- **[L]** Dos eventos sin `event_id` salen indistinguibles en `?desde=` (el contrato no incluye `recepcion_id`).

### Logs
- **[R]** Sin datos personales: solo `recepcion_id`, `event_id`, estado y el *tipo* de error de SQLite.

### Limitaciones conocidas del bloque
- **[L]** Sin límite de tamaño del cuerpo ni autenticación del webhook.
- **[L]** `mismo_contenido` compara bytes crudos: un reenvío con el mismo JSON pero distinto espaciado u orden de claves se marca como contenido distinto.
- **[L]** El cuerpo crudo (en `eventos` y `repeticiones`) contiene datos personales y no tiene política de retención ni borrado.
- **[L]** Un evento con `event_id` inválido (tipo no aceptado o vacío) se guarda con `event_id NULL` y pierde la deduplicación por `event_id`: si se repite, aunque sea con otro texto, cada envío crea una fila nueva.
- **[D]** Los sustitutos UTF-16 sueltos en `event_id` y `created_at` (p. ej. `"evt_\ud800"`) se escapan como texto (`evt_\\ud800`) con `backslashreplace`, de forma determinista, así el reenvío sigue siendo idempotente.
- **[D]** Si falla algo que no es SQLite al interpretar o insertar el evento, se guarda el cuerpo crudo como `rechazado`, con `event_id NULL` y motivo "No se pudo interpretar el evento", y se responde 2xx. Solo si ese guardado también falla se responde 503. En el log se registra únicamente el tipo de error.
- **[D]** Un `desde` válido pero fuera de rango al convertirlo a UTC (p. ej. `0001-01-01T00:00:00+01:00`) responde 400.
- **[L]** Se requiere Python 3.11 o superior: `datetime.fromisoformat` acepta el sufijo `Z` desde 3.11.
- **[L]** No hay validación, normalización, clasificación ni procesador de trabajos: todo queda `pendiente` (Bloque 2+).
- **[L]** `TestClient` de Starlette avisa de deprecación con `httpx`; no afecta a los tests.
