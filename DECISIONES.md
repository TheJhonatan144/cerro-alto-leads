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
- **[D]** Un error inesperado del bucle (p. ej. `database is locked`) se registra solo por su tipo y el hilo sigue vivo: espera un intervalo y vuelve a intentar. Desde el Bloque 2b.1 la espera también está dentro del manejo de errores.
- **[L]** Con un solo proceso de uvicorn. Si se lanzaran varios (`--workers N`), habría varios hilos: el reclamo atómico evita que dos tomen el mismo trabajo, pero se perdería la garantía de orden. Se debe ejecutar con un único proceso.

### Cola, reclamo y lease
- **[R]** `reclamar_trabajo(con, ahora)`: en `BEGIN IMMEDIATE` toma el trabajo `pendiente` más antiguo **por id** con `proximo_intento_at <= ahora`, lo pasa a `en_curso`, fija `lease_hasta = ahora + TRABAJO_LEASE_S` e incrementa `intentos`. El bloqueo de escritura garantiza que dos conexiones no reclamen el mismo trabajo.
- **[R]** `recuperar_expirados(con, ahora)`: los `en_curso` con `lease_hasta < ahora` vuelven a `pendiente` con `proximo_intento_at = ahora`. Se ejecuta al arrancar y en cada vuelta del bucle. El intento interrumpido cuenta como intento.
- **[D]** Si un trabajo con lease vencida ya agotó sus intentos (el proceso murió durante el último), pasa a `fallido` con error `LeaseVencido` en vez de volver a `pendiente`; así un evento que tumba el proceso no se reintenta para siempre.
- **[D]** Protección contra un dueño perdido: las escrituras del trabajador sobre un trabajo exigen `estado = 'en_curso'` y el mismo `intentos` que al reclamarlo. Si la lease venció y el trabajo se volvió a reclamar, el ejecutor anterior ya no puede cerrarlo. Desde el Bloque 2b.1 tampoco puede ejecutar más etapas (ver allí).
- **[D]** Todas las funciones reciben `ahora` por parámetro; los tests controlan el tiempo sin esperas reales (salvo los del hilo real).

### Etapas
- **[R]** `ETAPAS` es una lista ordenada de `Etapa(nombre, función)`. Por ahora una sola: `normalizar_y_duplicados` (llama a `procesar_evento`). Tras cada etapa se guarda su nombre en `trabajos.etapa` (última completada); al reanudar se sigue desde la siguiente. Las etapas futuras (API de pacientes, clasificación, revisión de mensajes de duplicados, avisos) se añaden a la lista sin tocar el resto.
- **[D]** Cada etapa gestiona sus propias transacciones y debe ser idempotente: la marca de etapa se escribe después, en otra transacción. Si el proceso muere entre ambas, la etapa se repite sin efecto. Así una etapa con una llamada externa no tiene que mantener el bloqueo de escritura de SQLite mientras espera la red.
- **[D]** Si `trabajos.etapa` tiene un nombre desconocido (p. ej. una etapa renombrada), se repiten todas: son idempotentes.
- **[D]** Cuando todas las etapas terminan, el trabajo queda `hecho`. El evento sigue `pendiente` (con su lead) hasta que existan las etapas que lo llevan a `procesado`.
- **[L]** Las etapas futuras deberán comprobar el estado del evento: si la primera lo dejó `rechazado` o `duplicado`, no deben actuar sobre él.

### Reintentos
- **[R]** Si una etapa lanza una excepción: se registra **solo el tipo** de error (sin el mensaje, que puede llevar datos del lead) y se guarda ese tipo en `ultimo_error`. Si `intentos < max_intentos`, vuelve a `pendiente` con `proximo_intento_at = ahora + espera` (desde el Bloque 2b.1, `ahora` = hora del fallo): **30 s, 2 min, 10 min, 30 min** (el último valor se repite).
- **[R]** Máximo **5 intentos** por defecto (`TRABAJO_MAX_INTENTOS`, guardado en `trabajos.max_intentos` al crear el trabajo). **Lease de 60 s** (`TRABAJO_LEASE_S`). Al agotarse: trabajo `fallido` y el evento pasa a `fallido` con motivo "Procesamiento fallido tras N intentos (<tipo de error>)", **solo si sigue `pendiente`**: nunca se pisa un `rechazado` ni un `duplicado`.
- **[L]** Un trabajo en espera de reintento no bloquea a los siguientes: los más nuevos se procesan mientras tanto, y el orden de llegada deja de cumplirse para ese evento.

### La etapa de duplicados va primero
- **[D]** `normalizar_y_duplicados` es la primera etapa, antes de cualquier llamada externa (API de pacientes, LLM). Solo usa SQLite y funciones puras, así que casi no puede fallar por causas externas, y el orden de llegada se respeta en la práctica.
- **[L]** La detección de duplicados sigue dependiendo del orden de llegada: si esta etapa fallara para un evento y se reintentara más tarde, los eventos posteriores de esa persona ya se habrían procesado sin verlo.

### Respaldo del webhook (cambio respecto al Bloque 1.2)
- **[D]** Si ocurre una excepción que no es de SQLite al interpretar el cuerpo durante la recepción, el cuerpo crudo se guarda como **`pendiente`** (antes `rechazado`), con `event_id NULL`, sin motivo y **con su trabajo**, para que el procesador lo intente por su cuenta con reintentos. Se registra solo el tipo de error. Si ese guardado también falla → 503.
- ~~**[L]** Ese evento pierde la deduplicación por `event_id`.~~ Corregido en el Bloque 2b.1: el trabajador vuelve a extraer el `event_id` (ver allí).

### Configuración y .env
- **[R]** Todo se valida en `create_app` (`config.cargar_config`). Un valor ausente o vacío usa el defecto; uno no vacío pero inválido (texto donde va un entero, o `<= 0`) hace **fallar el arranque** con un mensaje que nombra la variable, sin repetir su valor. Nunca falla al procesar cada evento: el trabajador recibe los valores ya validados.
- **[D]** `WORKER_ACTIVO` solo admite `0` o `1` (vacío = `1`). `DUPLICADO_VENTANA_DIAS = 0` ahora también es inválido (antes se aceptaba). Los rangos completos se fijaron en el Bloque 2b.1.
- **[R]** Variables nuevas, con defectos: `WORKER_ACTIVO=1`, `WORKER_INTERVALO_S=1`, `TRABAJO_LEASE_S=60`, `TRABAJO_MAX_INTENTOS=5`. Todas en `.env.example`, vacías.
- **[R]** Lector de `.env` con la librería estándar: líneas `CLAVE=VALOR`; ignora comentarios (`#`) y líneas vacías; no pisa variables ya definidas en el entorno. Busca primero `./.env` y luego `../.env` (relativos al directorio actual) y carga **solo el primero que encuentra**.
- **[D]** Admite además un prefijo `export ` y comillas que envuelven todo el valor. **[L]** No admite comentarios al final de la línea (`CLAVE=valor # nota` deja `valor # nota`) ni valores en varias líneas.
- **[D]** Los tests no leen el `.env` real: `tests/conftest.py` desactiva la búsqueda, limpia las variables y apaga el trabajador salvo donde un test lo activa.

### Base de datos
- **[R]** `trabajos` añade `etapa` (por defecto `'pendiente'` = ninguna completada), `max_intentos` (por defecto 5), `lease_hasta` y `CHECK (estado IN ('pendiente','en_curso','hecho','fallido'))`. Sin migraciones: hay que borrar `data/leads.db*` antes de arrancar.

### Limitaciones conocidas del 2b
- **[L]** Un trabajo en espera de reintento deja pasar a los siguientes: para ese evento se pierde el orden de llegada (y con él, la garantía de la detección de duplicados).
- **[L]** Hay que ejecutar un solo proceso de uvicorn (sin `--workers N`): con varios procesos habría varios trabajadores y se perdería el orden de llegada.
- **[L]** Las etapas futuras deben ignorar los eventos `rechazado` o `duplicado`: la primera etapa puede dejarlos así y el trabajo sigue su curso.
- **[L]** Sin migraciones: si cambia el esquema hay que borrar la base (`data/leads.db*`) antes de arrancar.

## Bloque 2b.1 · Correcciones tras la revisión del 2b

Cada punto se demostró primero con un test que fallaba contra el código del 2b y después se corrigió. Los cinco defectos se reprodujeron. El esquema de la base **no** cambió.

### 1. Re-extracción del event_id
- **Defecto reproducido:** si el webhook fallaba al extraer el `event_id` (respaldo del 2b), el evento quedaba con `event_id NULL` para siempre: no se podía consultar por `event_id` y un reenvío del conector creaba un segundo lead.
- **[D]** Primera etapa del trabajador (`procesar_evento`): si el evento no tiene `event_id`, se vuelve a extraer de `cuerpo_crudo` con la misma función que usa el webhook (`normalizar.normalizar_event_id`).
  - Si se obtiene, se asigna en la misma transacción con `UPDATE OR IGNORE`, de modo que decide la restricción `UNIQUE`, no un `SELECT` previo.
  - Si choca con el `UNIQUE`, el evento es un reenvío: queda `duplicado`, con `duplicado_de` = ese `event_id`, `lead_id` = el del original y motivo "Reenvío de un evento ya recibido (mismo event_id)", sin datos personales. Su propio `event_id` sigue `NULL`, porque lo tiene el original.
  - Si la nueva extracción lanza una excepción inesperada, cuenta como error de la etapa: reintentos y, al agotarlos, `fallido`. Nunca `rechazado`, y el cuerpo crudo se conserva para reprocesar.
  - Si el cuerpo realmente no trae `event_id`, el comportamiento no cambia (D3).
- **[L]** Si el reenvío se procesa antes que su original, el `lead_id` del duplicado queda `NULL`: el original aún no tiene lead. `duplicado_de` sí es siempre consultable.

### 2. Comprobación de dueño antes de cada etapa
- **Defecto reproducido:** un ejecutor que había perdido el trabajo (reclamado por otro, o con la lease vencida) seguía ejecutando etapas; solo fallaba al cerrar.
- **[D]** Antes de cada etapa se hace una escritura que exige el mismo `intentos`, `estado = 'en_curso'` y `lease_hasta >= ahora`, y que **renueva la lease** (`ahora + TRABAJO_LEASE_S`). Si afecta cero filas, se aborta sin ejecutar la etapa ("perdido"). Si guardar la etapa completada afecta cero filas, se interrumpe el recorrido.
- **[D]** El trabajo abandonado se recupera por lease y continúa desde la última etapa guardada, en un intento nuevo.
- **[L]** La comprobación ocurre entre etapas: una etapa que tarde más que la lease puede solaparse con otro ejecutor mientras corre. Por eso las etapas deben ser idempotentes, y la lease (60 s) debe ser mayor que la duración de cualquier etapa.

### 3. Rangos de configuración y espera del bucle
- **Defecto reproducido:** se aceptaban valores absurdos (p. ej. `WORKER_INTERVALO_S=100000000000000000000`), y con ese valor `Event.wait` lanzaba `OverflowError` fuera del `try` y **mataba el hilo**.
- **[D]** Rangos, ambos extremos incluidos; fuera de rango, el arranque falla con un mensaje que nombra la variable:

  | Variable | Tipo | Rango | Defecto |
  |---|---|---|---|
  | `DUPLICADO_VENTANA_DIAS` | entero | 1 a 90 | 7 |
  | `WORKER_INTERVALO_S` | decimal | 0.1 a 60 | 1 |
  | `TRABAJO_LEASE_S` | entero | 10 a 3600 | 60 |
  | `TRABAJO_MAX_INTENTOS` | entero | 1 a 20 | 5 |
  | `WORKER_ACTIVO` | 0 o 1 | — | 1 |

- **[D]** `WORKER_INTERVALO_S` pasa a admitir decimales con punto; se rechazan exponentes, `inf` y `nan`.
- **[D]** La espera del bucle está ahora dentro del `try`. Tras un error inesperado se espera una pausa fija de 1 s (`ESPERA_TRAS_ERROR_S`), que no depende de la configuración.

### 4. Cierre con plazo
- **Defecto reproducido:** si el hilo no terminaba dentro del plazo del `join`, se registraba igualmente "trabajador detenido".
- **[D]** Tras el `join` se comprueba `is_alive()`. Si sigue vivo, se registra el aviso "trabajador no terminó a tiempo; se recuperará por lease" (y no el mensaje de detenido), y `detener()` devuelve `False`. El trabajo a medias queda `en_curso` y se recupera cuando vence su lease.

### 5. Reintento desde la hora del fallo
- **Defecto reproducido:** la espera se contaba desde el reclamo. Una etapa que tardaba 40 s y fallaba quedaba programada para "reclamo + 30 s", ya en el pasado, y se **volvía a reclamar al instante**, gastando un intento sin esperar.
- **[D]** `ejecutar_trabajo` y `procesar_pendientes` aceptan un instante fijo o un reloj. El reloj se consulta antes de cada etapa y en el momento del fallo: `proximo_intento_at = hora del fallo + espera`.

## Bloque 3 · Decisiones aprobadas, pendientes de implementar

Estas decisiones estaban **aprobadas, pendientes de implementar en el Bloque 3**. **Ya están implementadas y probadas**: ver la sección "Bloque 3 · Enriquecimiento con la API de pacientes" más abajo, donde se detalla cómo quedó cada una.

- **D5 · Fallo de la API de pacientes** (timeout, 5xx): un reintento corto dentro de la etapa. Si sigue fallando, el lead continúa con `enriquecimiento=no_disponible` y se avisa por el canal de errores, para no bloquear el aviso a recepción de menos de 5 minutos.
  - **[L]** Ese lead no se enriquece después.
- **D6 · Límite de solicitudes:** limitador local de 25 solicitudes por minuto. Ante un 429 se respeta `Retry-After` reprogramando el trabajo, sin gastar intentos y sin esperar dentro de la etapa. Un 401 no se reintenta: se avisa y se degrada como en D5.
- **D7 · Varios pacientes con el mismo teléfono o correo:** solo se considera paciente si el nombre normalizado coincide. Si no, "coincidencia ambigua" y no se activa el criterio caliente de tratamiento pendiente.
- **Tope de páginas por consulta:** 5, con advertencia si se alcanza.

## Decisión transversal · Núcleo en código + n8n opcional

- **[D]** El núcleo del sistema (recepción, persistencia, idempotencia, cola, etapas, reintentos) está en código Python. n8n solo podrá usarse como adaptador **opcional** de avisos y errores (Bloque 5): el núcleo no depende de él, y si n8n falla, el aviso queda en la salida persistente y en archivo. Redis y microservicios, no.
- **Razones:** la idempotencia atómica (restricción `UNIQUE` + `ON CONFLICT` en una transacción), la cola persistente con lease y espera entre reintentos, los tests deterministas (con el reloj inyectado) y el arranque en 30 minutos son más fiables en código que en un flujo visual.
- **Equivalencias con n8n**, para quien conozca la herramienta:
  - *Error Trigger* = manejador central de errores del trabajador (registro del tipo de error, `ultimo_error`, paso a `fallido`).
  - *Wait* y reintentos = espera entre reintentos del trabajador (30 s, 2 min, 10 min, 30 min; máximo 5).
  - *IF / Switch* = enrutado por temperatura (bloque de clasificación).
- **Estado:** previsto para el Bloque 5; todavía no hay flujos de n8n en el repositorio ni se ha ejecutado ninguno. Cuando se incluyan, esta línea pasará a: "flujos de n8n incluidos como opción, probados por el autor, no requeridos" (solo después de haberlos ejecutado).

## Bloque 3 · Enriquecimiento con la API de pacientes

Implementado en `app/pacientes.py` (cliente, limitador, presupuesto y validación), `app/enriquecimiento.py` (etapa y persistencia) y `app/errores.py` (`Reprogramar` y el punto único del canal de errores). Probado solo contra una API simulada (`httpx.MockTransport`): **no se ha llamado a la API real**.

### Etapa y flujo
- **[R]** Nueva etapa `enriquecer_paciente` en `ETAPAS`, después de `normalizar_y_duplicados`. Solo actúa sobre eventos `pendiente` con lead; ignora `rechazado`, `duplicado` y `fallido` (el trabajo queda `hecho` sin llamar a la API).
- **[R]** Una sola consulta `GET /v1/pacientes` con el teléfono E.164 y el correo normalizados del lead (los que existan; la API devuelve los que coincidan con cualquiera). httpx codifica el `+` como `%2B` (verificado con un test). Paginación por `cursor`, hasta `PACIENTES_MAX_PAGINAS` (5); si queda página siguiente, advertencia `tope_paginas_pacientes`. *(Cambiado en el Bloque 3.2: una búsqueda incompleta da `ambigua`.)*
- **[R] D7** (refinada por D7.1 en el Bloque 3.1; ver allí): es paciente solo si exactamente **una** coincidencia tiene el mismo `clave_nombre` que el lead (minúsculas, sin tildes, espacios colapsados). Si hay candidatos pero ninguno o más de uno coincide por nombre → `ambigua`, sin pedir tratamientos. Sin candidatos → `no_paciente`.
- **[D]** Antes de aplicar D7 se eliminan los candidatos repetidos por `id`: el mismo paciente en dos páginas no hace ambigua la coincidencia. Un lead con nombre vacío nunca coincide por nombre.
- **[R]** Con coincidencia única se piden los tratamientos (máximo 5 páginas; si se alcanza el tope, advertencia `tope_paginas_tratamientos`). Se guardan el número de tratamientos `pendiente` y sus tipos distintos, en orden de aparición y como máximo 5. Si la API responde 404 a los tratamientos, el paciente queda encontrado, con `tratamientos_pendientes = NULL` y la advertencia `tratamientos_no_disponibles`. *(Cambiado en el Bloque 3.2: tratamientos incompletos y `tratamientos_completos`; tipos normalizados y validados.)*

### Cliente HTTP
- **[R]** httpx con timeout de conexión de 3 s y de lectura de 5 s, sin seguir redirecciones (una redirección cuenta como `respuesta_inesperada`). El token solo va en la cabecera `Authorization`.
- **[R]** Los loggers `httpx` y `httpcore` quedan en `WARNING`. **Probado con mutación:** si se quita ese ajuste, el test de logs falla, porque httpx registra en INFO la URL con `telefono=%2B593…&email=…`.
- **[D]** `trust_env=False`: el cliente no usa proxies ni `.netrc` del entorno. Si hiciera falta un proxy, habría que configurarlo explícitamente.
- **[D]** Las excepciones de httpx se convierten en códigos cortos con `raise … from None`, para que ningún mensaje encadenado lleve la URL con datos personales.
- **[R]** Se valida la forma de cada respuesta: objeto con `data` (lista) y `next_cursor` (`null` o texto no vacío); cada paciente con `id`, `nombre_completo` (texto), `estado` ∈ {activo, inactivo} y `sede_habitual` ∈ {norte, valle} o `null`; cada tratamiento con `tipo` (texto no vacío) y `estado` ∈ {pendiente, en_curso, completado, cancelado}. Cualquier desviación, o un JSON inválido, cuenta como API no disponible (`forma_inesperada`), nunca como excepción sin controlar. *(Cambiado en el Bloque 3.2: `next_cursor` ausente, `id` con patrón y campos guardados que pasan a NULL con advertencia.)*
- **[D]** Del paciente solo se conservan `id`, la clave del nombre (para D7, en memoria; no se guarda), sede, última visita y estado. El teléfono, el correo y la fecha de nacimiento que devuelve la API se descartan al validar.

### D5 · Fallos de la API
- **[R]** Ante un timeout, un error de red o un 5xx: **un solo reintento corto por ejecución de la etapa**, si quedan al menos 1 s de presupuesto. Si sigue fallando: `enriquecimiento = no_disponible`, con motivo legible y log WARNING sin datos personales. El lead continúa y el trabajo queda `hecho`: degradar no es fallar.
- **[D]** El reintento es inmediato, sin pausa: la etapa no duerme.
- **[R]** 401 (y también 403): sin reintento; `no_disponible` con motivo "La API de pacientes rechazó las credenciales" y log ERROR. 400 o forma inesperada: `no_disponible`.
- **[R]** El canal de errores tiene un único punto de llamada: `errores.reportar_error(origen, codigo, evento_id=…, nivel=…)`. Hoy solo registra en el log; el Bloque 5 lo enlazará con el canal de avisos.
- **[L]** Un lead `no_disponible` no se vuelve a enriquecer después (aprobado en D5).
- **[R]** Si `API_PACIENTES_URL` o `API_PACIENTES_TOKEN` están vacíos: `no_configurado`, sin llamadas, y un único aviso WARNING al arrancar (`create_app`).

### D6 · Límites y reprogramación
- **[R]** Limitador local de ventana deslizante, 25 solicitudes por minuto (`PACIENTES_LIMITE_POR_MIN`, de 1 a 30: la API admite 30 por token). Sin cupo, la etapa **no duerme**: lanza `Reprogramar(segundos)` con lo que falta para liberar un hueco.
- **[R]** Un 429 lanza `Reprogramar(Retry-After)` con tope de 120 s. **[D]** Si `Retry-After` falta, no es un entero o es ≤ 0, se usan 60 s. *(Cambiado en el Bloque 3.2: Retry-After exacto hasta 120 s; si es mayor, falta o es inválido, se degrada.)*
- **[R]** El trabajador, ante `Reprogramar`: deja el trabajo `pendiente` con `proximo_intento_at = ahora + espera`, libera la lease y **no consume intento** (deshace el incremento del reclamo). Como máximo 3 reprogramaciones por trabajo (columna nueva `trabajos.reprogramaciones`). A la cuarta, la etapa degrada según D5 ("Se alcanzó el límite de solicitudes de la API de pacientes"). Se mantiene la comprobación de dueño del 2b.1.
- **[D]** Si una etapa cualquiera lanza `Reprogramar` con las reprogramaciones agotadas, el trabajador lo trata como un fallo normal (`ReprogramacionesAgotadas`, con reintentos con espera). Es una red de seguridad para etapas futuras; `enriquecer_paciente` degrada antes.
- **[L]** El limitador vive en memoria, uno por proceso (compartido entre trabajos mediante el `Contexto` del trabajador): al reiniciar se vacía. El límite de 30/min de la API sigue protegiendo con sus 429.
- **[L]** Una reprogramación repite la etapa desde el principio: las consultas ya hechas en ese intento se vuelven a hacer.
- **[L]** Con muchos leads seguidos, al agotarse las 25 solicitudes del minuto los trabajos se reprograman hasta que se libera cupo: la cola puede retrasarse hasta un minuto, y un lead que necesite más de 3 reprogramaciones degrada a `no_disponible`.

### Presupuesto de tiempo
- **[R]** La etapa tiene un presupuesto total de 30 s (reloj monotónico; la lease es de 60 s y se renueva al empezar cada etapa). Cada petición usa `min(5 s, restante)` de lectura y `min(3 s, restante)` de conexión. Sin presupuesto, `no_disponible` ("Se agotó el tiempo disponible…").
- **[L]** El presupuesto se comprueba antes de cada petición y se aplica a cada timeout de httpx. No es un tope estricto de reloj de pared: lo que tarden la base o la validación entre peticiones no está acotado aparte, aunque es despreciable frente a la red.

### Persistencia
- **[R]** Nueva tabla `enriquecimientos`, una fila por evento (`evento_id` como clave primaria) con UPSERT: repetir la etapa actualiza la fila y no duplica. Columnas: `estado_enriquecimiento` (paciente | no_paciente | ambigua | no_disponible | no_configurado), `coincidencia` (unica | ninguna | ambigua), `paciente_id`, `sede_habitual`, `ultima_visita`, `estado_paciente`, `tratamientos_pendientes`, `tipos_pendientes` (JSON, máximo 5), `advertencias` (JSON), `motivo` y `consultado_at`, más `lead_id`. **No** se guardan el nombre, el teléfono, el correo ni la fecha de nacimiento del paciente.
- **[D]** `CHECK`: `paciente_id` presente si y solo si el estado es `paciente`; motivo obligatorio en `ambigua`, `no_disponible` y `no_configurado`.
- **[R] El esquema cambia** (tabla nueva y columna `trabajos.reprogramaciones`). Sin migraciones: hay que borrar `data/leads.db*` antes de arrancar.

### Configuración
- **[R]** Variables nuevas, validadas al arrancar como las demás:

  | Variable | Validación | Defecto |
  |---|---|---|
  | `API_PACIENTES_URL` | `http`/`https` con host; sin credenciales, consulta ni fragmento | vacía → `no_configurado` |
  | `API_PACIENTES_TOKEN` | ASCII imprimible sin espacios; el error no muestra el valor | vacía → `no_configurado` |
  | `PACIENTES_LIMITE_POR_MIN` | entero de 1 a 30 | 25 |
  | `PACIENTES_MAX_PAGINAS` | entero de 1 a 20 | 5 |

- **[D]** `Config` oculta el token en su `repr`, y `ClientePacientes` también: imprimir o registrar cualquiera de los dos no lo muestra.

### Tests y seguridad
- **[R]** `tests/conftest.py` bloquea (autouse) `connect`, `connect_ex` y `getaddrinfo` hacia cualquier host que no sea loopback. **[D]** El bloqueo lanza un `OSError`, como una red caída real, para que el código bajo prueba siga su camino de error real; un test comprueba que un cliente sin `MockTransport` degrada a `no_disponible` sin salir de la máquina.
- **[S]** El webhook registra el `event_id`. Se asume opaco, como en los payloads (`evt_…`); si el conector metiera datos personales en él, acabarían en el log.

## Bloque 3.1 · D7.1, cruce por nombre (refinamiento de D7)

- **Defecto reproducido:** D7 comparaba la clave del nombre con igualdad exacta. El lead "Carla Mendoza" frente al paciente "Carla Mendoza Ruiz" (el ejemplo del propio contrato de la API) quedaba `ambigua`. Antes de corregir fallaron 4 de los 9 casos del test de integración: el apellido extra, el orden invertido, las tildes con apellido extra y el caso simétrico. Los 5 ambiguos o exactos ya pasaban y quedan como regresión.
- **[R] D7.1** (`normalizar.elegir_por_nombre`, función pura con tests de tabla en `tests/test_nombres.py`):
  1. Se normaliza (sin tildes ni mayúsculas, espacios colapsados), se separa en palabras y se descartan las partículas `de, del, la, las, los, y`.
  2. Si exactamente **una** candidata tiene las mismas palabras (en cualquier orden), es el paciente. Si hay dos o más exactas, es ambigua. *(Cambiado en el Bloque 3.2: D7.2 elimina la prioridad de la exacta.)*
  3. Si no hay ninguna exacta: una candidata coincide si todas las palabras del nombre más corto están en el más largo **y** el más corto tiene al menos 2 palabras. La regla es simétrica: da igual si el nombre largo es el del lead o el de la API. Si exactamente una coincide así, es el paciente.
  4. Cualquier otro caso con candidatas: `coincidencia = "ambigua"`, sin pedir tratamientos. Un nombre de lead vacío, o de una sola palabra tras quitar partículas, nunca confirma a un paciente.
- **[D]** Las palabras se cuentan (multiconjunto): "Carla Carla" no está contenido en "Carla Mendoza".
- **[D]** Antes de aplicar la regla se siguen eliminando los candidatos repetidos por `id`.
- **[L]** Errores de ortografía ("Karla"), apodos ("Pepe" por José), nombres compuestos escritos juntos o apellidos cambiados producen `ambigua`. Es un falso negativo, el lado seguro: ese lead no recibe el criterio caliente de tratamiento pendiente, pero **nunca se atribuye a un lead el historial de otra persona**.
- **[L]** Dos personas distintas que comparten contacto y cuyos nombres se contienen (p. ej. "Juan Pérez" y "Juan Pérez López", padre e hijo con el mismo teléfono) se confundirían si solo una de ellas aparece como paciente. El riesgo se limita porque la API ya filtra por teléfono o correo.
- El esquema de la base **no** cambió.

## Bloque 3.2 · Correcciones tras la revisión del Bloque 3

Cada punto se demostró primero con un test que fallaba contra el código del 3.1 (`tests/test_pacientes_3_2.py` y la tabla de `tests/test_nombres.py`). Los **siete** defectos se reprodujeron. Los puntos 3, 4 y 5 son **cambios de regla**, no solo correcciones.

### 1. Presupuesto total y tope de tamaño
- **Defecto reproducido:** httpx limita cada lectura, no el total. Contra un servidor real en loopback que envía 1 byte cada 0,5 s, con un presupuesto de 2 s la etapa tardó **14,8 s** y terminó como si nada (`no_paciente`). Tampoco había tope de tamaño: un cuerpo de 300 KiB se aceptaba.
- **[D]** La respuesta se lee en streaming (`client.stream` e `iter_bytes`), y el presupuesto se comprueba en cada trozo. Pasado el plazo se aborta como `timeout` (transitorio): reintento corto si queda presupuesto y, si no, D5. Con la corrección, el mismo test termina en **2,07 s** con `no_disponible`.
- **[D]** El cuerpo tiene un tope de **256 KiB**, comprobado por `Content-Length` y también mientras se lee (sobre el contenido ya descomprimido, así que vale para gzip): `respuesta_demasiado_grande` → `no_disponible`. `httpx.DecodingError` cuenta como forma inesperada.
- **[L]** El exceso máximo sobre el presupuesto es **un timeout de lectura (5 s)**: un trozo puede tardar hasta ese tiempo en llegar antes de la comprobación. En el peor caso, unos 35 s por etapa, siempre por debajo de la lease de 60 s, que además se renueva al empezar cada etapa. *(Solo vale mientras se lee el cuerpo; ver la corrección siguiente.)*
- **[L] Corrección (hallazgo de revisión, no corregido):** mientras llegan las **cabeceras** el presupuesto no se comprueba: httpx solo limita cada lectura. Un servidor que envíe las cabeceras byte a byte, más deprisa que el timeout de lectura pero sin terminar nunca, puede alargar la petición sin límite y **superar la lease de 60 s**. Riesgo residual **aceptado**: exige un servidor anómalo, no hay pérdida de datos (el evento y su trabajo siguen persistidos) y lo peor es que la cola se detiene mientras dure. Se detectaría por "pendiente antiguo" en el resumen y la vigilancia previstos (Bloque 5b y Bloque 5n).

### 2. Resultados parciales
- **Defecto reproducido:** con el tope de páginas alcanzado y `next_cursor` pendiente, la búsqueda podía confirmar un paciente "único" que no lo era; y con los tratamientos incompletos, cero pendientes vistos se guardaban como `0`.
- **[D]** Búsqueda incompleta → `ambigua`, motivo "Búsqueda incompleta: se alcanzó el tope de páginas…", advertencia `tope_paginas_pacientes`, sin pedir tratamientos.
- **[D]** Columna nueva `enriquecimientos.tratamientos_completos`: `1` si se leyeron todas las páginas, `0` si se alcanzó el tope, `NULL` sin dato (404). Con el tope alcanzado y pendientes vistos, el conteo es un **mínimo** válido; con cero pendientes vistos, `tratamientos_pendientes = NULL` (desconocido), nunca `0`.

### 3. D7.2 (cambio de regla; refinamiento de D7.1)
- **Defecto reproducido:** D7.1 daba prioridad a la coincidencia exacta. Un padre "Juan Pérez" y su hijo "Juan Carlos Pérez", con el mismo contacto, daban "paciente = el padre" aunque el lead pudiera ser cualquiera de los dos.
- **[R] D7.2:** es paciente solo si exactamente **una** candidata es compatible, ya sea por igualdad exacta de palabras o por contención según D7.1 (el más corto con al menos 2 palabras y contenido en el más largo). Si hay más de una compatible, `ambigua`. Se eliminó la prioridad de la exacta. Los tests de D7.1 que esperaban "gana la exacta" ahora esperan `ambigua`.
- **[L]** Más casos ambiguos (falso negativo, el lado seguro): nunca se atribuye el historial de un familiar.

### 4. Retry-After (cambio de regla)
- **Defecto reproducido:** un `Retry-After` de 500 se recortaba a 120 y se reintentaba antes de tiempo, gastando reprogramaciones. Un valor ausente, inválido o `0` se convertía en 60 s.
- **[R]** Si el valor es ≤ 120 s, se reprograma **exactamente** ese tiempo (mínimo 1 s; `0` → 1 s). Si es mayor, no se reintenta antes de tiempo ni se gastan reprogramaciones: se degrada a `no_disponible` con motivo "Límite de uso de la API de pacientes…", y queda registro (`reportar_error`, código `limite_de_uso`).
- **[D]** Lectura segura: solo dígitos ASCII y como mucho 6 caracteres (se admiten espacios alrededor). Un entero enorme, un valor con signo, decimales o texto, y una cabecera **ausente**, se tratan como "mayor que el tope" (se degrada), sin `OverflowError` ni conversión de enteros gigantes. La cabecera ausente se trata así porque el contrato la promete en todo 429.

### 5. next_cursor ausente (cambio de regla)
- **Defecto reproducido:** si faltaba la clave `next_cursor`, la página se aceptaba en silencio como última, aunque viniera llena.
- **[R]** Sin la clave, solo se acepta si la página trae menos de 3 elementos (último tramo), con la advertencia `next_cursor_ausente`. Con 3 elementos y sin la clave → forma inesperada (degrada). Con la clave presente, sin cambios.

### 6. JSON anidado
- **Defecto reproducido:** un JSON anidado (50 000 niveles) lanzaba `RecursionError` sin capturar. La etapa fallaba, el trabajo gastaba intentos y no se guardaba nada.
- **[D]** El cuerpo se decodifica con `json.loads` y se captura `ValueError` (incluye UTF-8 inválido) y `RecursionError` como forma inesperada: `no_disponible`, trabajo `hecho`, sin gastar intentos.

### 7. Validación de lo que se guarda del paciente
- **Defecto reproducido:** `ultima_visita = "Rosa Quishpe, teléfono 0987650021"` se guardaba tal cual. Una sede o un estado desconocidos tumbaban toda la respuesta. Los tipos de tratamiento se guardaban sin validar.
- **[D]** `ultima_visita`: fecha ISO `YYYY-MM-DD` real (`2026-02-30` y `2026-3-1` no valen). `sede_habitual` ∈ {norte, valle}; estado del paciente ∈ {activo, inactivo}. Un valor inválido se guarda como `NULL` con advertencia (`ultima_visita_invalida`, `sede_invalida`, `estado_paciente_invalido`), **sin guardar ni registrar el texto original** (verificado sobre el volcado completo de la base y los logs).
- **[D]** Tipos de tratamiento: se normalizan (minúsculas, sin tildes, espacios colapsados) y deben cumplir `^[a-z0-9_ ]{1,40}$`. Un tipo inválido no se guarda (advertencia `tipo_tratamiento_invalido`), pero el tratamiento **sí cuenta** como pendiente. Los tipos guardados quedan en minúsculas (`"ortodoncia"`).
- **[D] Desviación de lo pedido:** un `paciente_id` que no cumple `^[A-Za-z0-9_-]{1,64}$` **no** queda como `NULL` con advertencia: sin id no se puede identificar al paciente ni pedir sus tratamientos (y el `CHECK` exige id en `paciente`). Por eso toda la respuesta cuenta como forma inesperada → `no_disponible`, y el id inválido no se guarda.
- **[D] Patrón del id (ajuste del 3.2):** `^[A-Za-z0-9_-]{1,64}$` (con `fullmatch`). Su único objetivo es que el id sea **seguro dentro de la URL** `/v1/pacientes/{id}/tratamientos`; **no** asume el prefijo `pac_`, que no está verificado contra la API real. Válidos: `pac_8F2K1`, `P-123`, `123`, 64 caracteres. Inválidos, que degradan toda la respuesta sin construir nunca la URL: con `/`, `?`, `#`, `%`, `.` (incluido `../x`), espacios, saltos de línea (también uno final), caracteres no ASCII, vacío o 65 caracteres o más. El id se sigue codificando con `quote(…, safe='')` como segunda barrera.
- **[L]** Un id numérico en el JSON (p. ej. `123` sin comillas) se rechaza: el contrato no fija el tipo y se exige texto. Si la API real devolviera números, habría que aceptarlos convirtiéndolos a texto (`PATRON_PACIENTE_ID` y `_validar_paciente` en `app/pacientes.py`).

### Esquema
- **Cambia:** columna nueva `enriquecimientos.tratamientos_completos`. Sin migraciones: hay que borrar `data/leads.db*` antes de arrancar.

## Diferido tras la revisión del Bloque 3

Puntos señalados en la revisión que se dejan como están, de forma deliberada:

- **[L] Tipo de tratamiento:** se acepta cualquier texto corto en minúsculas que cumpla `^[a-z0-9_ ]{1,40}$` tras normalizar. Es un dato de la API de la propia clínica, así que la minimización es parcial: un texto corto que no sea un tipo real (p. ej. un nombre en minúsculas) se guardaría.
- **[D] `next_cursor` ausente en página corta:** se acepta con la advertencia `next_cursor_ausente`. Es una tolerancia deliberada: si la API real omitiera el cursor en las respuestas vacías o cortas, una validación estricta degradaría a `no_disponible` a todos los leads. Se revisará tras verificar la API real.

## Fase 1 frente a lo construido

Diferencias entre lo que suponía la Fase 1 y lo implementado:

- **[D] Duplicados:** la Fase 1 suponía "mismo teléfono o correo en 24 h". El sistema usa una ventana de 7 días (`DUPLICADO_VENTANA_DIAS`) y exige además el mismo nombre (D1; reglas en la sección del Bloque 2a), para no fusionar a familias que comparten teléfono o correo.
- **[L] Webhook sin autenticar:** la documentación del conector no incluye firma ni secreto compartido, y el conector no se puede modificar. Queda como limitación conocida (ya listada en el Bloque 1, aquí con su motivo).
- **[L] `GET /leads/estado` sin autenticación:** devuelve `telefono_e164` y `email` sin autenticar porque el contrato exige esos campos. Es una limitación del contrato, no una omisión.

## Plan restante

**Previsto; nada de esta sección está ejecutado.**

- **Bloque 4 · Clasificación:**
  - clasificación con el LLM y validación de su respuesta;
  - reglas deterministas como piso (el LLM no puede bajar un lead por debajo de lo que dictan las reglas);
  - enmascarado de datos personales antes de enviar texto al LLM;
  - revisión de los mensajes de duplicados (`lead_mensajes`, D4).
- **Bloque 5 · Enrutado y avisos:**
  - enrutado por temperatura y cita tentativa con las reglas de la ficha;
  - avisos con adaptador intercambiable (Telegram, archivo o n8n);
  - política de reintentos propia para los avisos de leads calientes, con un aviso por evento;
  - manejador central de errores, que enlaza `errores.reportar_error`.
- **Bloque 5b · Resumen:** `GET /leads/resumen` y un comando equivalente.
- **Bloque 5n · Flujos de n8n** (concreta el "Bloque 5" citado en la decisión transversal de n8n):
  - **Avisos:** Webhook → Telegram.
  - **Errores:** Webhook → Telegram, más un *Error Trigger* real para los fallos de los propios flujos de n8n.
  - **Vigilancia:** Schedule Trigger → HTTP → IF → Telegram. Detecta caída del servidor, fallidos o pendientes antiguos y falta de actividad en horario (informativo), y envía un resumen diario.
  - Los flujos solo se declararán "probados por el autor" **después de ejecutarlos**.
- **Bloque 6 · Cierre:** reproceso de fallidos y de `no_disponible`, `README`, `USO_IA` y tabla final.
