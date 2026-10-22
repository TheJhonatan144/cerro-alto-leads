# Dashboard de leads · Clínica Dental Cerro Alto

Una sola pantalla para la coordinadora comercial, con las cinco preguntas del enunciado y las anomalías que aparecen en los últimos 90 días (30 jun – 27 sep 2026, 3.890 leads).

**Abrir:** `index.html` (un solo archivo, funciona sin servidor y sin conexión; Chart.js va incluido).
Si activas GitHub Pages sobre el repositorio (Settings → Pages → rama `main`, carpeta raíz), queda en `https://<usuario>.github.io/<repo>/dashboard/`.

## Qué responde

| # | Pregunta | Gráfico | Por qué ese gráfico |
|---|----------|---------|---------------------|
| 1 | Leads por día, plataforma y campaña | Columnas apiladas por día (Facebook/Instagram) + barras agrupadas por campaña + promedio por día de la semana | Es una serie de tiempo con dos partes que suman el total: las columnas apiladas muestran volumen y mezcla a la vez, y dejan ver a simple vista un día en cero. |
| 2 | Caliente / tibio / basura por campaña | Columnas apiladas al 100 % | La pregunta es de proporciones, no de volumen; al 100 % se comparan campañas de tamaño distinto. |
| 3 | Calientes que terminan en cita y mejor campaña | Barras con intervalo de confianza 95 % (calientes y tibios) | Tres campañas con 200–360 calientes cada una: sin el intervalo se vería un «ganador» que en parte es azar. |
| 4 | ¿Se cumple el aviso en menos de 5 min? | Histograma con línea en 5 min + % en plazo por semana | Un promedio esconde la cola larga; el histograma muestra quién se pasa y la línea semanal muestra si empeora. |
| 5 | Salud del sistema | Cuatro cifras de estado + columnas apiladas por día y motivo | Las cifras dan el tamaño del problema; el gráfico diario muestra *cuándo* ocurre, que es lo que permite ver una racha. |

Cada gráfico tiene tooltip al pasar el cursor, leyenda, descripción para lectores de pantalla y una tabla («Ver como tabla») con los mismos datos. Hay modo claro y oscuro (botón arriba a la derecha) y se lee en celular desde 375 px.

## Anomalías marcadas

1. **Instagram en cero del 18 al 21 de agosto** (99,9 h sin un solo lead; ≈97 leads, ≈21 calientes y ≈15 citas esperados). Alerta propuesta: «0 leads de una plataforma durante 5 horas hábiles seguidas»; habría saltado el 18/08 a las 13:00 y no se dispara en falso en el resto del período.
2. **Racha de timeouts de la API de pacientes del 8 al 10 de septiembre** (17 timeouts; 13,3 % de leads fallidos contra 1,8 % normal). Alerta propuesta: «3 timeouts en 3 horas»; habría saltado el 08/09 a las 14:34, sin falsas alarmas.
3. **Lunes 14 de septiembre, 128 leads** (≈1,9 veces un lunes normal). Observación, no falla: no cambió el plazo de aviso ni los fallos; los datos no explican la causa.

Las causas son hipótesis: con este archivo no se pueden confirmar. Las dos alertas se probaron contra los 90 días (dispara en la anomalía y no en días normales); hoy el sistema de la Parte A no tiene la alerta de «cero leads» (`/leads/resumen` no la incluye), y es la mejora que habría detectado la primera anomalía.

## Cómo se genera

Todos los números que aparecen en la página salen del CSV; ninguno está escrito a mano.

```bash
pip install pandas
python generar_dashboard.py            # lee leads_90_dias.csv y escribe index.html
```

- `generar_dashboard.py`: calcula las cifras (porcentajes, intervalos de Wilson, huecos, alertas simuladas) y rellena la plantilla.
- `plantilla.html`: estructura, estilos y gráficos.
- `vendor/chart.umd.js`: Chart.js 4.4.1 (licencia MIT), incluido para que el HTML no dependa de internet.
- `capturar.py`: genera las capturas de `capturas/` con Playwright (opcional).

## Decisiones y límites

- «Plazo de aviso» solo existe para calientes (`minutos_hasta_aviso`); por eso solo ellos se miden.
- Duplicados, rechazados y fallidos no tienen clasificación y se excluyen de la pregunta 2; no son fallas del sistema salvo los fallidos.
- Las horas se muestran como vienen en el archivo (Quito, -05:00).
- Colores validados con un verificador de paletas (distinguibles con daltonismo, contraste sobre la superficie claro y oscuro); el significado nunca depende solo del color: hay leyendas, etiquetas y tablas.
- No hay un servidor: es una foto de los 90 días. Para verlo en vivo habría que conectarlo a la base de datos de la Parte A (fuera del alcance de esta entrega).
