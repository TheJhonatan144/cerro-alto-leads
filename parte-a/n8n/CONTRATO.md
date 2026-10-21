# Contrato con n8n

Este documento describe **exactamente** lo que el sistema envía a n8n y la forma de
`GET /leads/resumen`. Los ejemplos de `ejemplos/` se generan desde el código
(`python -m app.ejemplos_n8n`, en `parte-a/`) y un test (`tests/test_contrato_n8n.py`) comprueba
que coinciden. Todos los datos de los ejemplos son ficticios.

n8n es un **adaptador opcional** (decisión "Núcleo en código + n8n opcional"): el núcleo no
depende de él. Si n8n falla, el aviso sigue en la bandeja y se reintenta; con
`AVISOS_CANAL=n8n,archivo` también queda en el archivo.

## 1. Webhook de avisos (`N8N_AVISOS_URL`)

`POST`, JSON, `Content-Type: application/json`. Si `N8N_SECRETO` está configurado, la petición
lleva la cabecera `X-Webhook-Secret: <secreto>`; el flujo de n8n debe comprobarla.

```json
{
  "tipo": "lead_caliente" | "novedad_duplicado",
  "evento_id": 101,
  "texto": "texto plano del aviso, varias líneas, sin Markdown",
  "creado_at": "2026-11-02T14:58:11.000000Z"
}
```

- `evento_id`: id interno del evento (entero), no el `event_id` del conector.
- `texto`: el mismo texto que va a Telegram o al archivo. Lleva datos personales (nombre y
  contacto), pero **nunca** el texto libre del lead.
- Ejemplos: [`ejemplos/aviso_caliente.json`](ejemplos/aviso_caliente.json) y
  [`ejemplos/novedad_duplicado.json`](ejemplos/novedad_duplicado.json).

## 2. Webhook de errores (`N8N_ERRORES_URL`)

`POST`, JSON, con la misma cabecera `X-Webhook-Secret` si está configurada.

```json
{"codigo": "aviso_vencido", "detalle": "origen=avisos evento=101", "ocurrencias": 1,
 "ts": "2026-11-02T14:58:11.000000Z"}
```

- `codigo`: uno del catálogo de `DECISIONES.md` (D19).
- `detalle`: solo códigos, contadores e ids de evento; nunca texto libre ni datos personales.
- `ocurrencias`: número de veces que se repitió el error dentro de la ventana (`ERRORES_VENTANA_S`).
- `ts`: momento de la primera ocurrencia.
- Ejemplo: [`ejemplos/error.json`](ejemplos/error.json).

## 3. Cómo interpreta el sistema la respuesta de n8n

| Respuesta | Resultado |
|---|---|
| 2xx | entregado (se leen como mucho 64 KiB del cuerpo, que no se usa) |
| 404, 408, 429, 5xx, timeout (10 s) o error de red | **temporal**: se reintenta. Un flujo inactivo devuelve 404 y es recuperable |
| 400, 401, 403, 422 y demás 4xx; redirecciones (no se siguen) | **permanente**: el aviso queda `fallido` |

Semántica de **al menos una vez**: n8n puede recibir el mismo aviso más de una vez (por
ejemplo, si el sistema se cae entre el envío y el registro de "enviado"). Usar `tipo` +
`evento_id` como clave de deduplicación.

## 4. `GET /leads/resumen?desde=<ISO 8601 con zona>`

Sin autenticación (igual que `/leads/estado`) y **sin datos personales**. Por defecto cubre
las últimas 24 h (por `received_at`). Un `desde` inválido o sin zona horaria devuelve 400 con
un mensaje genérico.

```json
{
  "generado_at": "…Z", "desde": "…Z", "salud": "ok" | "degradado",
  "eventos": {"total": 0, "por_estado": {"procesado": 0, "duplicado": 0, "rechazado": 0, "fallido": 0, "pendiente": 0}},
  "rutas": {"caliente": 0, "tibio": 0, "basura": 0},
  "pendiente_mas_antiguo_s": null,
  "avisos": {"pendientes": 0, "enviados": 0, "fallidos": 0, "vencidos": 0,
             "latencia_s": {"p50": null, "max": null}, "en_plazo": {"cumplen": 0, "total": 0}},
  "seguimientos_pendientes": 0, "incidencias_abiertas": 0,
  "hilos": {"trabajador": true, "despachador": true},
  "alertas": []
}
```

- `alertas` ⊆ `pendiente_antiguo`, `aviso_vencido`, `aviso_fallido`, `eventos_fallidos`,
  `incidencias_pendientes`, `hilo_caido`. `salud` es `"ok"` solo si no hay alertas.
- `hilos` vale `null` si el hilo no está configurado para ejecutarse (`WORKER_ACTIVO=0`).
- Ejemplo: [`ejemplos/resumen.json`](ejemplos/resumen.json).

Un flujo de vigilancia en n8n (Schedule Trigger → HTTP → IF → aviso) puede consultar este
endpoint y avisar si `salud` es `"degradado"`.
