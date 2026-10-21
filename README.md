# Sistema de leads · Clínica Dental Cerro Alto

Parte A de la prueba técnica de INVERS (clínica ficticia). Recibe los leads del conector por
webhook, los guarda **antes** de responder, los procesa en una cola persistente (normalizar y
detectar duplicados → enriquecer con la API de pacientes → clasificar → enrutar) y avisa a
recepción de los leads calientes.

- **Stack:** Python 3.12, FastAPI, SQLite (módulo `sqlite3` estándar, sin ORM), httpx.
- **Funciona sin configurar nada:** sin `.env`, el LLM queda en `none` (clasificación solo con
  reglas), la API de pacientes queda "no configurada" (`enriquecimiento=no_configurado`) y los
  avisos se escriben en un archivo local.
- **Decisiones, supuestos y limitaciones:** [`DECISIONES.md`](DECISIONES.md).
  **Uso de IA:** [`USO_IA.md`](USO_IA.md).

```
README.md               este archivo
DECISIONES.md           decisiones (D1…D22), supuestos y limitaciones
USO_IA.md               cómo se usó la IA en el proyecto
.env.example            variables del sistema, sin valores reales (todas opcionales)
payloads/               los 10 eventos de ejemplo del conector (no modificar)
enviar_payloads.py      script original de la prueba (no modificar)
parte-a/
  app/                  código (main.py = API; trabajador.py = cola; cli.py = operación)
  prompts/              prompt del LLM, versionado (clasificar_lead_v2.md)
  tests/                tests (pytest) y esperado_payloads.json
  scripts/              verificar_payloads.py y prueba_humo_real.py
  n8n/                  flujos opcionales de n8n y su contrato (CONTRATO.md)
  requirements.txt
```

---

## 1. Instalación en una máquina limpia

Requisitos: **Python 3.12** (sirve 3.11 o superior), `git` y `curl` (solo para las consultas
de ejemplo; también sirve el navegador). Linux, macOS o WSL. Los comandos son para bash; en
Windows nativo, activa el venv con `.venv\Scripts\activate`. `pip install` necesita acceso a
PyPI; el sistema, una vez instalado, no necesita red.

```bash
git clone <URL-del-repositorio> cerro-alto-leads
cd cerro-alto-leads/parte-a          # TODOS los comandos siguientes se ejecutan desde parte-a/
python3.12 -m venv .venv             # si no existe "python3.12": python3 --version, y si es ≥ 3.11, python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuración (opcional)

```bash
cp ../.env.example ../.env           # el .env va en la RAÍZ del repositorio
```

Con todas las variables vacías el sistema funciona. Cada variable está explicada en
[`.env.example`](.env.example). El programa busca primero `./.env` y luego `../.env` (relativos al
directorio actual), así que arrancando desde `parte-a/` lee el `.env` de la raíz. Una variable
del entorno de la shell tiene prioridad sobre el `.env`. Un valor mal escrito o fuera de rango
**impide arrancar**, con un mensaje que nombra la variable (nunca su valor).

`.env` está en `.gitignore`: nunca lo subas.

## 2. Tests

```bash
python -m pytest -q
```

Resultado esperado: todos en verde (**1071 passed** en el cierre del Bloque 6; tarda alrededor de
un minuto). Los tests no leen tu `.env`, bloquean cualquier conexión que no sea a loopback y usan
dobles para la API de pacientes, el LLM, Telegram y n8n. El aviso de deprecación de
`TestClient`/`httpx` es conocido y no afecta.

## 3. Arrancar el servidor

```bash
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

- Escucha solo en `127.0.0.1`. Usa **un solo proceso** (no pases `--workers`): el trabajador de
  la cola y el despachador de avisos son hilos dentro de ese proceso.
- Al arrancar, crea `parte-a/data/leads.db` (SQLite en modo WAL) si no existe. `data/` está
  ignorado por git.
- Los avisos de `WARNING` del arranque son normales sin configuración: dicen que la API de
  pacientes no está configurada, que `LLM_PROVIDER=none` y que los avisos solo van a archivo.
- Para apagar: `Ctrl+C`. El apagado espera a que los hilos terminen. Un trabajo cortado a la
  mitad no se pierde: se retoma al volver a arrancar, cuando vence su lease (60 s).
- Si el puerto 8000 está ocupado, usa otro (`--port 8001`) y pásalo también a los scripts
  (`--url http://127.0.0.1:8001`).

Endpoints:

| Método y ruta | Qué hace |
|---|---|
| `POST /webhook/leads` | Recibe un evento. **202** si es nuevo, **200** si el `event_id` ya existía (devuelve el estado original), **413** si el cuerpo supera `WEBHOOK_MAX_BYTES`, **503** si no se pudo guardar (nunca un ack falso). |
| `GET /leads/estado?event_id=X` | Estado de un evento (8 campos del contrato más `recepcion_id`). 404 si no existe. |
| `GET /leads/estado?desde=ISO` | Todos los eventos recibidos desde esa fecha, en orden de llegada. |
| `GET /leads/resumen[?desde=ISO-con-zona]` | Resumen operativo **sin datos personales** (por defecto, últimas 24 h). |
| `GET /salud` | `{"status": "ok"}` |

## 4. Enviar los payloads y comprobar el resultado

Con el servidor arrancado, abre **otra terminal** (la primera queda ocupada por uvicorn):

```bash
cd cerro-alto-leads/parte-a
source .venv/bin/activate
python scripts/verificar_payloads.py                         # puerto 8000
python scripts/verificar_payloads.py --url http://127.0.0.1:8001   # si arrancaste en otro puerto
```

Envía los 10 archivos de `payloads/` en orden (pausa de 1 s), espera a que cada evento deje de
estar `pendiente` (como mucho 60 s por evento) y compara estado y ruta con
`tests/esperado_payloads.json`. Imprime solo archivo, estado y ruta. Sale con código 0 si todo
coincide y 1 si algo no coincide. Opciones: `--url`, `--pausa`, `--timeout`.

Salida esperada con `LLM_PROVIDER=none`:

```
archivo         estado     ruta      resultado
evento_01.json  procesado  caliente  OK
evento_02.json  procesado  caliente  OK     (mismo event_id que el 01: 200 con el estado del 01)
evento_03.json  duplicado  -         OK     (mismo nombre y contacto que el 01)
evento_04.json  procesado  tibio     OK
evento_05.json  procesado  tibio     OK
evento_06.json  procesado  tibio     OK
evento_07.json  procesado  caliente  OK
evento_08.json  procesado  basura    OK
evento_09.json  procesado  tibio     OK
evento_10.json  procesado  tibio     OK
10/10 coinciden
```

(La salida real va separada por tabuladores y sin los comentarios entre paréntesis.) Se puede
repetir contra la misma base: por la idempotencia por `event_id`, el resultado es el mismo.
Con un LLM configurado las rutas pueden variar, porque el LLM puede subir un lead de tibio a
caliente; `esperado_payloads.json` supone `LLM_PROVIDER=none`.

El script original de la prueba también funciona (desde la raíz del repositorio):
`python3 enviar_payloads.py http://127.0.0.1:8000/webhook/leads`.

### Consultar el estado y el resumen

```bash
curl -s "http://127.0.0.1:8000/leads/estado?event_id=evt_01JB7Q3M2X4K8R9T5W6Y"
curl -s "http://127.0.0.1:8000/leads/estado?desde=2020-01-01T00:00:00Z"
curl -s "http://127.0.0.1:8000/leads/resumen"
```

`/leads/estado` devuelve teléfono y correo normalizados, porque el contrato los pide. **No tiene
autenticación**: no lo expongas fuera de `127.0.0.1`.

Los avisos de los leads calientes quedan en `parte-a/data/avisos.jsonl` (una línea JSON por
aviso; **contiene datos personales**, permisos 0600) y las incidencias, en
`parte-a/data/errores.jsonl` (sin datos personales).

## 5. CLI de operación

Desde `parte-a/`, con el servidor parado o en marcha (SQLite en WAL admite ambas cosas). No
arranca hilos. Usa la base de `DB_PATH` (por defecto `data/leads.db`) o la de `--db RUTA`, y
nunca crea una base vacía.

```bash
python -m app.cli resumen [--desde ISO] [--json]    # los mismos números que /leads/resumen
python -m app.cli listar-fallidos                   # event_id, etapa, intentos, último error
python -m app.cli reprocesar --event-id X [--dry-run]
python -m app.cli reprocesar --todos [--dry-run]
python -m app.cli reintentar-avisos                 # avisos fallidos -> pendiente
python -m app.cli seguimientos [--limite N]         # mensajes para los tibios (¡datos personales!)
```

- `reprocesar` devuelve a `pendiente`, **en una sola transacción**, los eventos `fallido` y su
  trabajo (con los intentos a 0). El trabajo se reanuda tras la última etapa completada. No toca
  un trabajo en curso con lease vigente (es de un trabajador vivo). Es idempotente: repetirlo no
  duplica avisos, seguimientos ni citas. Con `--dry-run` solo muestra lo que haría. Un
  `event_id` inexistente sale con código 1. El servidor (si está en marcha) recoge el evento en
  el siguiente ciclo; si está parado, al arrancar.
- `listar-fallidos` y `reprocesar` no muestran datos personales.

## 6. Canales de aviso

`AVISOS_CANAL` (avisos de leads calientes) y `ERRORES_CANAL` (incidencias) aceptan una lista
separada por comas de `archivo`, `telegram` y `n8n`. Si quedan vacías, valen `archivo`.

| Canal | Configuración | Qué hace |
|---|---|---|
| `archivo` (por defecto) | `AVISOS_ARCHIVO`, `ERRORES_ARCHIVO` (opcionales) | Una línea JSON por aviso o incidencia. Si es el único canal, el archivo **es** la entrega. Si hay otro canal, el archivo es solo auditoría. |
| `telegram` (opcional) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `sendMessage` en texto plano. |
| `n8n` (opcional) | `N8N_AVISOS_URL`, `N8N_ERRORES_URL`, `N8N_SECRETO` (opcional) | `POST` JSON al webhook de n8n, con la cabecera `X-Webhook-Secret` si hay secreto. |

Un canal elegido sin su configuración impide arrancar. Un aviso queda `enviado` solo cuando lo
acepta al menos un canal que llega a una persona (Telegram o n8n). Si fallan todos, se reintenta
(5 s, 15 s, 45 s, 90 s y luego cada 120 s, hasta 10 intentos). Si pasan 5 minutos desde la
recepción sin enviarse, se registra una incidencia `aviso_vencido` (D13, D18).

> Telegram y n8n se probaron en los tests solo con dobles (MockTransport y un servidor de
> loopback). Contra servicios reales, solo los probó el autor.

## 7. LLM (opcional)

Sin LLM (`LLM_PROVIDER` vacío o `none`), el sistema clasifica solo con reglas deterministas.
Para activarlo:

```
LLM_PROVIDER=anthropic        # o gemini, u openai
LLM_MODEL=<nombre exacto del modelo según tu proveedor>
LLM_API_KEY=<tu clave>
```

- Al LLM solo le llega el mensaje **enmascarado** (sin teléfonos, correos, documentos, enlaces ni
  el nombre), el servicio y la campaña. El texto del lead es un dato, nunca una instrucción.
- Su salida se valida contra un esquema estricto. Las reglas son un piso: el LLM no puede bajar
  un lead que las reglas marcan como caliente, y solo puede marcar basura con intención spam y
  confianza ≥ `LLM_CONFIANZA_BASURA`.
- Si el LLM falla, el lead se clasifica solo con reglas y se registra una incidencia. Nunca
  queda sin ruta.
- El prompt está versionado en `parte-a/prompts/clasificar_lead_v2.md`. Cada clasificación
  guarda su versión y su sha256.

### Prueba de humo con servicios reales (la ejecuta el autor)

```bash
python scripts/prueba_humo_real.py           # API de pacientes: como mucho 2 peticiones
python scripts/prueba_humo_real.py --llm     # además, UNA petición al LLM
```

Usa los clientes de la propia app. Busca un teléfono ficticio y, solo si encuentra exactamente
un paciente, pide sus tratamientos. Con `--llm`, manda un mensaje ficticio ya enmascarado.
Imprime solo códigos HTTP, nombres de claves del JSON, si el validador aceptó la salida del LLM
y la ruta resultante; nunca tokens, URLs ni cuerpos. Los tests la prueban solo con dobles.

## 8. Flujos de n8n (opcionales; probados por el autor)

El núcleo **no depende de n8n**. Si n8n falla, el aviso sigue en la bandeja de la base, se
reintenta y queda en el archivo de auditoría. Los flujos están en `parte-a/n8n/`:

| Archivo | Flujo |
|---|---|
| `avisos.json` | Webhook (con autenticación por cabecera) → Switch por `tipo` → Telegram → respuesta |
| `errores.json` | Webhook → Telegram, más un *Error Trigger* para los fallos de los propios flujos |
| `vigilancia.json` | Schedule → `GET /leads/resumen` → IF `salud` → Telegram, más un resumen diario |

Para usarlos:

1. Importa cada JSON en n8n (*Import from file*).
2. Crea las credenciales en n8n: una de tipo *Header Auth* con nombre de cabecera
   `X-Webhook-Secret` y el mismo valor que `N8N_SECRETO`, y otra de Telegram con tu bot.
3. Sustituye `REEMPLAZAR_CHAT_ID` por tu chat. Si n8n corre en Docker y el sistema en el
   anfitrión, la URL de vigilancia `http://host.docker.internal:8000/leads/resumen` ya apunta
   al anfitrión. Si no, cámbiala. En ese caso arranca uvicorn con un `--host` accesible desde
   el contenedor, sabiendo que `/leads/estado` y `/leads/resumen` no tienen autenticación.
4. Activa los flujos y pon sus URLs de producción en `N8N_AVISOS_URL` y `N8N_ERRORES_URL`,
   con `AVISOS_CANAL=n8n,archivo` y `ERRORES_CANAL=n8n,archivo`.

El contrato exacto (JSON enviado, códigos de respuesta y forma de `/leads/resumen`) está en
[`parte-a/n8n/CONTRATO.md`](parte-a/n8n/CONTRATO.md), con ejemplos generados desde el código y
comprobados por `tests/test_contrato_n8n.py`.

## 9. Supuestos principales

Están detallados en `DECISIONES.md` (marcados con **[S]** y **[D]**). Los más importantes:

- El conector reintenta si no recibe 2xx en 10 s: por eso un 503 es seguro y nunca se responde
  2xx sin haber guardado.
- **Duplicado de negocio** (D1): mismo nombre normalizado y mismo teléfono o correo en una
  ventana de 7 días (`DUPLICADO_VENTANA_DIAS`). Es distinto del reenvío del mismo `event_id`
  (idempotencia, restricción `UNIQUE` en la base).
- Un paciente de la API cuenta como "el mismo" solo si además coincide el nombre (D7).
- Horario, sedes y precios de la cita tentativa salen de las reglas de la ficha. No se consulta
  una agenda real (D12).
- Zona horaria de la clínica: Ecuador continental (UTC-5, sin horario de verano).

## 10. Qué puede fallar y cómo se recupera

| Fallo | Qué hace el sistema | Qué haces tú |
|---|---|---|
| La base no admite escrituras al recibir | 503 con `Retry-After: 5`, nada guardado | Nada: el conector reintenta |
| El proceso se cae a mitad de un trabajo | El trabajo queda `en_curso`; al vencer la lease (60 s) se retoma | Volver a arrancar |
| Una etapa lanza un error | Reintentos a los 30 s, 2 min, 10 min y 30 min; al 5.º fallo, el evento queda `fallido` y se registra la incidencia `evento_fallido` | `python -m app.cli listar-fallidos` y `reprocesar` |
| API de pacientes caída o lenta | Un reintento corto; después `enriquecimiento=no_disponible` y el lead sigue (D5) | Nada. Ese lead no se vuelve a enriquecer (ver limitaciones) |
| API de pacientes con 429 | Se reprograma según `Retry-After` sin gastar intentos (D6) | Nada |
| El LLM falla o responde algo inválido | Clasificación solo con reglas e incidencia `llm` | Revisar la clave o el modelo |
| Telegram o n8n fallan | Reintentos hasta 10 y después aviso `fallido`; incidencia si pasan 5 min | `python -m app.cli reintentar-avisos` |
| Se cae el hilo trabajador o el despachador | El otro hilo lo detecta: incidencia `hilo_caido` y alerta en `/leads/resumen` | Reiniciar el servidor |
| Eventos pendientes demasiado tiempo | Incidencia `pendiente_antiguo` y alerta en el resumen | Revisar el resumen y los logs |
| Cuerpo demasiado grande | 413, no se guarda | Nada |
| Configuración inválida | No arranca; el mensaje nombra la variable | Corregir el `.env` |

Los logs nunca llevan datos personales, tokens ni URLs con datos: solo ids, estados y tipos de
error.

## 11. Limitaciones conocidas

La lista completa está en `DECISIONES.md` ("Limitaciones conocidas y lo que dejé fuera"). En
resumen:

- El webhook, `/leads/estado` y `/leads/resumen` **no tienen autenticación**. El contrato del
  conector no prevé firma ni secreto. Pensado para `127.0.0.1` o detrás de un proxy.
- El cuerpo crudo de cada evento (con datos personales) se guarda sin política de retención.
- Un lead con `enriquecimiento=no_disponible` no se vuelve a enriquecer. `reprocesar` no lo
  hace: solo recupera eventos `fallido`.
- Al reprocesar un caliente tarde, su aviso nace ya vencido (el plazo de 5 min cuenta desde la
  recepción) y genera la incidencia `aviso_vencido`. Un evento que falló en la primera etapa y
  se reprocesa más tarde no se marca como duplicado de eventos posteriores de la misma persona.
- Entrega "al menos una vez": un aviso puede llegar dos veces si el proceso muere entre el
  envío y su registro.
- Los mensajes de seguimiento de los tibios se redactan y se consultan con la CLI, pero **no se
  envían**.
- Probado con un solo proceso de uvicorn.
- El formato de petición y respuesta de cada proveedor de LLM y la API de pacientes real solo
  se probaron con dobles en los tests. Contra los servicios reales, solo con
  `prueba_humo_real.py`, que ejecuta el autor.
