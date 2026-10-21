# DECISIONES

Leyenda: **[R]** requisito confirmado · **[D]** decisión propia · **[S]** supuesto · **[L]** limitación conocida.

## Bloque 1 · Recepción, persistencia e idempotencia

### Persistencia
- **[R]** Se persiste antes de responder 2xx. El 202 se devuelve solo después del `COMMIT`; cualquier `sqlite3.Error` (al conectar, insertar o confirmar) responde **503** con `Retry-After: 5`, nunca un ack falso.
- **[R]** SQLite con `sqlite3` estándar, `journal_mode=WAL`, `busy_timeout=5000 ms` y `synchronous=FULL` en cada conexión.
- **[D]** Una conexión por petición, transacción `BEGIN IMMEDIATE` (toma el bloqueo de escritura al inicio, así las escrituras concurrentes se serializan y esperan vía `busy_timeout` en vez de fallar a mitad).
- **[D]** `received_at` se toma *dentro* de la transacción, así es monótono con el `id` (orden de llegada real = orden de commit). Formato fijo `YYYY-MM-DDTHH:MM:SS.ffffffZ`, validado con `CHECK ... GLOB`; por ser de ancho fijo, comparar texto equivale a comparar fechas.
- **[D]** `cuerpo_crudo` es `BLOB`: guarda los bytes exactos, incluso si no son UTF-8 válido. `hash_cuerpo` = SHA-256 de esos bytes.
- **[D]** `DB_PATH` relativo se resuelve contra `parte-a/`, no contra el directorio actual (por defecto `parte-a/data/leads.db`, ignorado por git). Las variables se leen del entorno y, desde el Bloque 2b, también de un `.env` (ver Bloque 2b).
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
- ~~**[D]** `trabajos.estado` aún sin CHECK.~~ Desde el Bloque 2b: `CHECK (estado IN ('pendiente','en_curso','hecho','fallido'))`.

### GET /leads/estado
- **[R]** Los 8 campos del contrato (confirmados por el enunciado) son: `event_id, estado, ruta, lead_id, duplicado_de, telefono_e164, email, motivo`.
- **[S]** `?desde=` filtra por `received_at` (momento en que **nosotros** recibimos el evento), **no** por `created_at` del origen, que no está validado y puede venir mal o en otra zona.
- **[D]** `desde` acepta ISO 8601 con `Z` o desplazamiento; sin zona = UTC; se convierte a UTC. Un `+` sin codificar en la URL llega como espacio (`...T10:00:00 05:00`) y se interpreta como `+05:00`. Formato inválido → 400.
- **[D]** Orden de llegada = `ORDER BY id`. Respuesta = arreglo JSON de objetos con los 8 campos más `recepcion_id` (ver Bloque 2a).
- **[D]** `event_id` y `desde` a la vez → 400 (son consultas distintas). Ninguno → 400. `event_id` vacío → 400. Desconocido → 404.
- ~~**[L]** Dos eventos sin `event_id` salen indistinguibles en `?desde=`.~~ Resuelto en el Bloque 2a con el campo extra `recepcion_id`.

### Logs
- **[R]** Sin datos personales: solo `recepcion_id`, `event_id`, estado y el *tipo* de error de SQLite.

### Limitaciones conocidas del bloque
- **[L]** Sin límite de tamaño del cuerpo ni autenticación del webhook.
- **[L]** `mismo_contenido` compara bytes crudos: un reenvío con el mismo JSON pero distinto espaciado u orden de claves se marca como contenido distinto.
- **[L]** El cuerpo crudo (en `eventos` y `repeticiones`) contiene datos personales y no tiene política de retención ni borrado.
- **[L]** Un evento con `event_id` inválido (tipo no aceptado o vacío) se guarda con `event_id NULL` y pierde la deduplicación por `event_id`: si se repite, aunque sea con otro texto, cada envío crea una fila nueva.
- **[D]** Los sustitutos UTF-16 sueltos en `event_id` y `created_at` (p. ej. `"evt_\ud800"`) se escapan como texto (`evt_\\ud800`) con `backslashreplace`, de forma determinista, así el reenvío sigue siendo idempotente.
- ~~**[D]** Si falla algo que no es SQLite al interpretar el evento, se guarda como `rechazado`.~~ Cambiado en el Bloque 2b: ahora se guarda como `pendiente` y con trabajo (ver "Respaldo del webhook" en el Bloque 2b).
- **[D]** Un `desde` válido pero fuera de rango al convertirlo a UTC (p. ej. `0001-01-01T00:00:00+01:00`) responde 400.
- **[L]** Colisión del escape: un `event_id` que llegue literalmente como el texto `evt_\ud800` (con barra invertida; en JSON `"evt_\\ud800"`) queda igual que uno con el sustituto real `"evt_\ud800"`, y se trataría como el mismo evento. Es muy improbable en la práctica; se acepta.
- **[L]** Se requiere Python 3.11 o superior: `datetime.fromisoformat` acepta el sufijo `Z` desde 3.11.
- **[L]** `TestClient` de Starlette avisa de deprecación con `httpx`; no afecta a los tests.

## Bloque 2a · Normalización, validación y duplicados de negocio

Implementado en `app/normalizar.py` (funciones puras) y `app/procesador.py` (`procesar_evento(con, evento_id)`). Desde el Bloque 2b lo llama el trabajador como primera etapa (`normalizar_y_duplicados`).

### Tipo de evento y estructura
- **[R]** `event_type` ausente o distinto de `"lead.created"` (comparación exacta) → `rechazado`, motivo "Tipo de evento no soportado".
- **[R]** `lead` ausente o que no es un objeto → `rechazado`, motivo "El evento no trae datos del lead".
- **[D]** Si el cuerpo crudo de un evento `pendiente` no se puede reinterpretar como objeto (no debería ocurrir, ya se filtró en la recepción) → `rechazado`, motivo "No se pudo interpretar el evento".
- **[D]** `source` ausente o que no es un objeto se trata como vacío: no rechaza, solo genera advertencias.

### Teléfono
- **[R]** Se quitan espacios, guiones, paréntesis y puntos (los puntos desde el Bloque 2b: `"099.123.4567"` → `+593991234567`); región por defecto EC. Se acepta `str` o `int` (no booleano); otro tipo → inválido. Tras limpiar solo se admiten `+` inicial y dígitos ASCII (así `"abc"` → None sin depender de la librería).
- **[R]** 12 dígitos sin `+` que empiezan con `593` = prefijo de país sin `+` (el entero `593987650021` → `+593987650021`).
- **[R]** El número se guarda en E.164 solo si `phonenumbers.is_valid_number` lo da por válido; `"1111111111"` → None.
- **[Probado]** `+1 (305) 555-0142`: con `phonenumbers==9.0.41`, `is_valid_number=True` → `+13055550142`. Por eso **no** se implementó la vía de respaldo (`is_possible_number` + advertencia `telefono_no_verificado`): no hace falta para los datos dados, y aceptar números solo "posibles" relajaría la validación. Si una versión futura de la librería lo diera por inválido, el evento 04 se aceptaría igual por su correo, con la advertencia `telefono_invalido`.

### Correo, nombre, textos y catálogos
- **[R]** Correo ASCII estricto (Bloque 2a.1; la regex simple anterior admitía `a\u0000@example.com` y `a@b..com`). Se recorta y pasa a minúsculas antes de validar; si no cumple, es None. Reglas:
  - solo ASCII, sin caracteres de control ni espacios; máximo 254 caracteres; exactamente una `@`;
  - parte local de 1 a 64 caracteres de `A-Za-z0-9` y `.!#$%&'*+/=?^_`{|}~-`, sin punto al inicio ni al final ni dos puntos seguidos;
  - dominio de hasta 253 caracteres con al menos dos etiquetas de 1 a 63 caracteres `[a-z0-9-]` que no empiezan ni terminan en `-`; la última es alfabética de 2 o más letras, o empieza por `xn--`.
  - Sin librerías de validación ni consultas de dominio.
- **[D]** Por qué ASCII estricto: el correo se usa como clave de duplicados y como dato de contacto para recepción y la API de pacientes. Un carácter de control o un dominio mal formado no sirve para contactar a nadie y puede provocar coincidencias falsas o errores aguas abajo. Se rechazan los correos internacionalizados (`ñ@example.com`): son raros en este contexto, y si el teléfono es válido el lead se acepta igual, con la advertencia `correo_invalido`. Los dominios IDN se admiten en su forma punycode (`xn--`).
- **[D]** La comprobación ASCII se hace **antes** de `.lower()`: el signo Kelvin `K` (U+212A) pasaría a `k` ASCII y se colaría.
- **[D]** En nombre, ciudad, mensaje, etc. los sustitutos UTF-16 sueltos se escapan con `backslashreplace`, como en el Bloque 1.
- **[R]** Nombre con espacios colapsados (`""` si no es texto). `clave_nombre`: minúsculas, sin tildes (NFKD), espacios colapsados; solo para comparar.
- **[R]** Texto: `str` recortado; lista de `str` unida con `", "` (el evento 06 trae `city: ["Quito"]` → `"Quito"`). **[D]** Vacío tras recortar → None.
- **[R]** Servicios, plataformas y campañas conocidos están en constantes (`normalizar.py`), tomados de los payloads de ejemplo. Se comparan con `clave_nombre` y se guarda el valor canónico. Un valor desconocido no rechaza: se guarda tal cual y se añade una advertencia.

### Rechazo por contacto y advertencias
- **[R]** Ni teléfono ni correo válidos → `rechazado`, con un motivo que indica qué falló sin copiar valores, p. ej. "Sin teléfono ni correo válidos: teléfono inválido, correo ausente". Ausente = falta, `null` o texto vacío; inválido = cualquier otro valor que no se pudo normalizar.
- **[R]** Nombre vacío y mensaje vacío o ausente **no** rechazan.
- **[D]** Advertencias posibles (JSON en `leads.advertencias`): `telefono_ausente`, `telefono_invalido`, `correo_ausente`, `correo_invalido`, `nombre_vacio`, `mensaje_vacio`, `ciudad_ausente`, `servicio_ausente`, `servicio_desconocido`, `plataforma_ausente`, `plataforma_desconocida`, `campana_ausente`, `campana_desconocida`.
- **[R]** Aceptado: se guardan `telefono_e164` y `email` normalizados en `eventos` (None si inválido), se crea la fila en `leads` con `lead_id` nuevo (uuid4) y se rellena `eventos.lead_id`. El estado queda `pendiente` hasta las etapas siguientes. El evento 08 (spam evidente) se acepta aquí por su correo válido; filtrarlo es tarea de la clasificación.

### Duplicados de negocio
- **[R]** Un evento es duplicado de uno anterior si coincide el teléfono normalizado **o** el correo normalizado, **y** coincide `clave_nombre` (no vacía en ambos), **y** el original se recibió dentro de los `DUPLICADO_VENTANA_DIAS` días (por defecto 7) anteriores a la recepción del actual, **y** el original no es `rechazado` ni `duplicado`.
- **[R]** Compartir solo teléfono o solo correo con otro nombre **no** es duplicado. Si cualquiera de los dos nombres está vacío, **no** es duplicado.
- **[R]** Se toma el original más antiguo (`ORDER BY received_at, id`). Duplicado → `estado='duplicado'`, `duplicado_de` = `event_id` del original, `lead_id` = el del original, motivo "Mismo contacto y nombre que un evento anterior (<received_at del original>)", sin datos personales. No crea fila en `leads`.
- **[D]** Un original sin `event_id` no genera duplicados (Bloque 2a.1). Solo cuentan como originales los eventos con `event_id`, para que `duplicado_de` sea siempre consultable con `GET /leads/estado?event_id=`. Ante la duda se conserva el lead: si el original no tiene `event_id`, el evento nuevo se acepta como lead propio, aunque eso pueda causar una llamada de más. Ya no se usa `recepcion_id` en `duplicado_de`.
- **[D]** El duplicado también guarda su `telefono_e164` y `email` normalizados en `eventos`.
- **[D] D4 · Un duplicado no pierde información nueva** (Bloque 2a.1). El duplicado no genera un lead nuevo ni una llamada nueva, pero su mensaje y su servicio normalizados se guardan en la tabla `lead_mensajes` (`lead_id` del original, `evento_id` UNIQUE, `recibido_at`, `servicio`, `mensaje`, `creado_at`). El estado público del duplicado no cambia: sigue `duplicado`, con su motivo.
  - **[L]** Pendiente: en el bloque de clasificación y avisos se añadirá una etapa que revise estos mensajes y avise a recepción como "actualización de un lead existente" si hay urgencia o intención de agendar (p. ej. "Solo quiero precios" seguido de "Tengo sangrado abundante y quiero atención inmediata"). Hasta entonces el mensaje queda guardado y consultable en `lead_mensajes`, pero nadie recibe aviso.
- **[D]** Ventana inclusiva: `received_at` del original ≥ `received_at` actual − N días. "Anterior" = `id` menor (orden de llegada). Solo se buscan originales entre eventos con fila en `leads`, es decir, aceptados.
- **[L]** El resultado depende del orden de procesamiento: si un evento posterior se procesa antes que uno anterior, el anterior no lo verá como original. El trabajador del Bloque 2b procesa en orden de llegada (ver allí).
- ~~**[L]** Un `DUPLICADO_VENTANA_DIAS` inválido hace fallar `procesar_evento`.~~ Desde el Bloque 2b se valida al arrancar: un valor inválido (o `<= 0`) impide arrancar.

### Idempotencia del procesamiento
- **[R]** `procesar_evento` corre en una sola transacción `BEGIN IMMEDIATE`. Solo actúa si el evento está `pendiente` y sin `lead_id`; si no, devuelve el estado actual sin tocar nada. Además, `leads.evento_id` es `UNIQUE`: procesar dos veces el mismo evento no puede crear dos filas.

### Campo extra en /leads/estado
- **[D]** `GET /leads/estado` (tanto `?event_id=` como cada elemento de `?desde=`) devuelve los 8 campos del contrato más `recepcion_id`. Los 8 campos se mantienen sin cambios; el extra permite distinguir eventos sin `event_id`.

### Base de datos
- **[D]** Nuevas tablas `leads` y `lead_mensajes` (sin migraciones: hay que borrar `data/leads.db*` antes de arrancar). `CHECK` de que haya teléfono o correo y de que `advertencias` sea JSON válido; índices por teléfono, correo y `clave_nombre`. La tabla `eventos` no cambió.

## Bloque 2b · Procesador de trabajos

Implementado en `app/trabajador.py`. **[L]** Aún no hay API de pacientes, LLM ni avisos.

### Un solo hilo
- **[D]** Un único hilo demonio, iniciado en el `lifespan` de FastAPI si `WORKER_ACTIVO` no es `0` y detenido limpiamente al apagar (con un `Event` y `join`). Cuando no hay trabajo espera `WORKER_INTERVALO_S` con `Event.wait`, así se despierta al instante al apagar.
- **Por qué un solo hilo:** la detección de duplicados necesita ver los eventos en orden de llegada (un evento solo puede ser duplicado de uno anterior ya procesado). Con varios hilos, dos eventos de la misma persona podrían procesarse a la vez y ninguno vería al otro. El volumen de una clínica no necesita más paralelismo.
- **[D]** Un error inesperado del bucle (p. ej. `database is locked`) se registra solo por su tipo y el hilo sigue vivo: espera un intervalo y vuelve a intentar.
- **[L]** Con un solo proceso de uvicorn. Si se lanzaran varios (`--workers N`), habría varios hilos: el reclamo atómico evita que dos tomen el mismo trabajo, pero se perdería la garantía de orden. Se debe ejecutar con un único proceso.

### Cola, reclamo y lease
- **[R]** `reclamar_trabajo(con, ahora)`: en `BEGIN IMMEDIATE` toma el trabajo `pendiente` más antiguo **por id** con `proximo_intento_at <= ahora`, lo pasa a `en_curso`, fija `lease_hasta = ahora + TRABAJO_LEASE_S` e incrementa `intentos`. El bloqueo de escritura garantiza que dos conexiones no reclamen el mismo trabajo.
- **[R]** `recuperar_expirados(con, ahora)`: los `en_curso` con `lease_hasta < ahora` vuelven a `pendiente` con `proximo_intento_at = ahora`. Se ejecuta al arrancar y en cada vuelta del bucle. El intento interrumpido cuenta como intento.
- **[D]** Si un trabajo con lease vencida ya agotó sus intentos (el proceso murió durante el último), pasa a `fallido` con error `LeaseVencido` en vez de volver a `pendiente`; así un evento que tumba el proceso no se reintenta para siempre.
- **[D]** Protección contra un dueño perdido: las escrituras del trabajador sobre un trabajo exigen `estado = 'en_curso'` y el mismo `intentos` que al reclamarlo. Si la lease venció y el trabajo se volvió a reclamar, el ejecutor anterior ya no puede cerrarlo.
- **[D]** Todas las funciones reciben `ahora` por parámetro; los tests controlan el tiempo sin esperas reales (salvo los del hilo real).

### Etapas
- **[R]** `ETAPAS` es una lista ordenada de `Etapa(nombre, función)`. Por ahora una sola: `normalizar_y_duplicados` (llama a `procesar_evento`). Tras cada etapa se guarda su nombre en `trabajos.etapa` (última completada); al reanudar se sigue desde la siguiente. Las etapas futuras (API de pacientes, clasificación, revisión de mensajes de duplicados, avisos) se añaden a la lista sin tocar el resto.
- **[D]** Cada etapa gestiona sus propias transacciones y debe ser idempotente: la marca de etapa se escribe después, en otra transacción. Si el proceso muere entre ambas, la etapa se repite sin efecto. Así una etapa con una llamada externa no tiene que mantener el bloqueo de escritura de SQLite mientras espera la red.
- **[D]** Si `trabajos.etapa` tiene un nombre desconocido (p. ej. una etapa renombrada), se repiten todas: son idempotentes.
- **[D]** Cuando todas las etapas terminan, el trabajo queda `hecho`. El evento sigue `pendiente` (con su lead) hasta que existan las etapas que lo llevan a `procesado`.
- **[L]** Las etapas futuras deberán comprobar el estado del evento: si la primera lo dejó `rechazado` o `duplicado`, no deben actuar sobre él.

### Reintentos
- **[R]** Si una etapa lanza una excepción: se registra **solo el tipo** de error (sin el mensaje, que puede llevar datos del lead) y se guarda ese tipo en `ultimo_error`. Si `intentos < max_intentos`, vuelve a `pendiente` con `proximo_intento_at = ahora + espera`: **30 s, 2 min, 10 min, 30 min** (el último valor se repite).
- **[R]** Máximo **5 intentos** por defecto (`TRABAJO_MAX_INTENTOS`, guardado en `trabajos.max_intentos` al crear el trabajo). **Lease de 60 s** (`TRABAJO_LEASE_S`). Al agotarse: trabajo `fallido` y el evento pasa a `fallido` con motivo "Procesamiento fallido tras N intentos (<tipo de error>)", **solo si sigue `pendiente`**: nunca se pisa un `rechazado` ni un `duplicado`.
- **[L]** Un trabajo en espera de reintento no bloquea a los siguientes: los más nuevos se procesan mientras tanto, y el orden de llegada deja de cumplirse para ese evento.

### La etapa de duplicados va primero
- **[D]** `normalizar_y_duplicados` es la primera etapa, antes de cualquier llamada externa (API de pacientes, LLM). Solo usa SQLite y funciones puras, así que casi no puede fallar por causas externas, y el orden de llegada se respeta en la práctica.
- **[L]** La detección de duplicados sigue dependiendo del orden de llegada: si esta etapa fallara para un evento y se reintentara más tarde, los eventos posteriores de esa persona ya se habrían procesado sin verlo.

### Respaldo del webhook (cambio respecto al Bloque 1.2)
- **[D]** Si ocurre una excepción que no es de SQLite al interpretar el cuerpo durante la recepción, el cuerpo crudo se guarda como **`pendiente`** (antes `rechazado`), con `event_id NULL`, sin motivo y **con su trabajo**, para que el procesador lo intente por su cuenta con reintentos. Se registra solo el tipo de error. Si ese guardado también falla → 503.
- **[L]** Ese evento pierde la deduplicación por `event_id`: si el conector lo reenvía, se guarda otra fila.

### Configuración y .env
- **[R]** Todo se valida en `create_app` (`config.cargar_config`). Un valor ausente o vacío usa el defecto; uno no vacío pero inválido (texto donde va un entero, o `<= 0`) hace **fallar el arranque** con un mensaje que nombra la variable, sin repetir su valor. Nunca falla al procesar cada evento: el trabajador recibe los valores ya validados.
- **[D]** `WORKER_ACTIVO` solo admite `0` o `1` (vacío = `1`). `DUPLICADO_VENTANA_DIAS = 0` ahora también es inválido (antes se aceptaba).
- **[R]** Variables nuevas, con defectos: `WORKER_ACTIVO=1`, `WORKER_INTERVALO_S=1`, `TRABAJO_LEASE_S=60`, `TRABAJO_MAX_INTENTOS=5`. Todas en `.env.example`, vacías.
- **[R]** Lector de `.env` con la librería estándar: líneas `CLAVE=VALOR`; ignora comentarios (`#`) y líneas vacías; no pisa variables ya definidas en el entorno. Busca primero `./.env` y luego `../.env` (relativos al directorio actual) y carga **solo el primero que encuentra**.
- **[D]** Admite además un prefijo `export ` y comillas que envuelven todo el valor. **[L]** No admite comentarios al final de la línea (`CLAVE=valor # nota` deja `valor # nota`) ni valores en varias líneas.
- **[D]** Los tests no leen el `.env` real: `tests/conftest.py` desactiva la búsqueda, limpia las variables y apaga el trabajador salvo donde un test lo activa.

### Base de datos
- **[R]** `trabajos` añade `etapa` (por defecto `'pendiente'` = ninguna completada), `max_intentos` (por defecto 5), `lease_hasta` y `CHECK (estado IN ('pendiente','en_curso','hecho','fallido'))`. Sin migraciones: hay que borrar `data/leads.db*` antes de arrancar.
