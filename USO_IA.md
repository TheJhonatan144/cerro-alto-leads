# Uso de IA

Cómo se usó la IA para construir la Parte A. Las secciones de verificación personal y de tiempo
las rellenó el autor con datos reales que él declara; no son estimaciones del asistente.

## Reparto de papeles

| Papel | Quién | Qué hizo |
|---|---|---|
| Autor | Jhonatan Torres | Definió los requisitos y el orden de los bloques. Aprobó o rechazó cada decisión. Ejecutó los tests en su máquina, hizo **todos** los commits y ejecutó las pruebas con servicios reales (API de pacientes, LLM, Telegram y n8n). |
| Implementador | Claude Code (Anthropic) | Escribió el código, los tests y los documentos de cada bloque, siguiendo las reglas de `CLAUDE.md`. Presentó la salida real de cada comando que ejecutó. |
| Revisor | ChatGPT (OpenAI) | Revisó cada bloque a partir del traspaso que entregaba el implementador y propuso correcciones. Las correcciones aceptadas se convirtieron en los subbloques "x.1" y "x.2" (ver `DECISIONES.md`). |

## Forma de trabajo

1. **Bloques pequeños:** cada bloque tenía objetivo, cambios, cómo ejecutar y resultado
   esperado. Al cerrarlo, el implementador entregaba un traspaso para el revisor: decisiones,
   archivos, pruebas con su salida real, pendientes y próximo paso.
2. **Tests primero:** cada corrección empezaba con un test que fallaba contra el código
   anterior. `DECISIONES.md` indica en cada bloque qué defectos se reprodujeron así.
3. **Mutaciones:** en los bloques críticos se rompió el código a propósito, en una copia
   temporal, para comprobar que algún test lo detectaba (p. ej. la tabla del "Paso 1" en
   `DECISIONES.md`).
4. **Distinción explícita** entre requisito, decisión, supuesto, implementado y probado
   (leyenda [R]/[D]/[S]/[L] en `DECISIONES.md`).

## Límites que se impusieron a la IA (`CLAUDE.md`)

- No leer, pedir ni imprimir tokens o claves. El `.env` nunca se sube.
- No llamar a la API de pacientes real, a un LLM real ni a otros servicios externos: los tests
  usan dobles y bloquean cualquier conexión fuera de loopback (`tests/conftest.py`).
- No modificar `payloads/` ni `enviar_payloads.py`.
- No hacer commits ni push. No añadir atribución a la IA en los commits.
- No afirmar que algo funciona sin haberlo ejecutado.
- Sin datos personales en los logs; el texto del lead es dato, nunca instrucción.

## Qué NO hizo la IA

- No ejecutó `parte-a/scripts/prueba_humo_real.py` ni ninguna prueba contra la API de
  pacientes real, un LLM real, Telegram o n8n. Esas pruebas las hizo el autor.
- No probó los flujos de n8n en una instancia real: los flujos están marcados como "probados
  por el autor".
- No hizo commits.

## Dónde la IA se equivocó y cómo se detectó

Algunos ejemplos documentados en `DECISIONES.md`:

- **D18:** en la primera versión, el archivo de auditoría contaba como canal de entrega. Un
  aviso podía quedar "enviado" sin llegar a nadie. Lo mostró la prueba final del 5b, se
  documentó primero como limitación y la revisión lo reclasificó como defecto. Se corrigió en
  el 5b.1.
- **Vigilancia:** el trabajador reportaba un falso `hilo_caido` al arrancar. Se demostró con un
  test que fallaba 3 de 3 y se corrigió.
- **Mutaciones del 5a:** tres mutaciones sobrevivían a los tests. Se añadieron tests hasta que
  las 21 se detectaron.

## Verificación personal del autor

Lo que verificó personalmente el autor (cada casilla marcada la ejecutó o comprobó él):

- [x] Ejecuté `python -m pytest -q` en mi máquina. Resultado: 1125 passed, 1 warning (≈ 41 s), 21-oct.
- [x] Seguí el README en un directorio limpio (clon del último commit, sin `.env`): instalar dependencias, `pytest` (1144 passed), arrancar, enviar los payloads y `verificar_payloads` (10/10), 21-oct.
- [x] Ejecuté `scripts/prueba_humo_real.py` (sin `--llm`) con mi token. Resultado (solo códigos y claves): búsqueda HTTP 200, claves `data` y `next_cursor`, 0 elementos; tratamientos omitidos (no había exactamente un paciente). `--llm` NO se ejecutó.
- [x] Probé los flujos de n8n (avisos, errores y vigilancia) con mi propio Telegram. Resultado: los tres funcionaron; avisos de lead caliente recibidos y mensaje de vigilancia recibido con el servidor caído.
- [x] Leí los archivos de `parte-a/app/` (con distinta profundidad).
- [ ] Revisé el historial de git en busca de secretos (ver Bloque 6.6 en `DECISIONES.md`).

## Tiempo

Horas reales declaradas por el autor (no se midió el tiempo por bloque):

| Bloque | Horas reales |
|---|---|
| Fase 1 (diseño) | ≈ 2 h |
| Bloques 1-2 (recepción, normalización y cola) | no medido por bloque |
| Bloque 3 (API de pacientes) | no medido por bloque |
| Bloque 4 (clasificación) | no medido por bloque |
| Bloque 5 (enrutado, avisos, errores y n8n) | no medido por bloque |
| Bloque 6 (cierre) | no medido por bloque |
| **Total** | Parte A entre 12,5 y 13,5 h + Fase 1 ≈ 2 h |
