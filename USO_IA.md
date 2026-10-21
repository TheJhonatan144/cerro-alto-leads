# Uso de IA

Cómo se usó la IA para construir la Parte A. Los campos **[COMPLETAR POR EL AUTOR]** los
rellena el autor con datos reales: no se estiman.

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

**[COMPLETAR POR EL AUTOR]** Qué verificó personalmente, por ejemplo:

- [ ] Ejecuté `python -m pytest -q` en mi máquina. Resultado: [COMPLETAR POR EL AUTOR]
- [ ] Seguí el README en una máquina o directorio limpio. Resultado: [COMPLETAR POR EL AUTOR]
- [ ] Ejecuté `scripts/prueba_humo_real.py` (y `--llm`) con mis credenciales. Resultado (solo
      códigos y claves): [COMPLETAR POR EL AUTOR]
- [ ] Probé los flujos de n8n (avisos, errores y vigilancia) con Telegram. Resultado:
      [COMPLETAR POR EL AUTOR]
- [ ] Revisé el código de: [COMPLETAR POR EL AUTOR]
- [ ] Revisé el historial de git en busca de secretos (ver Bloque 6.6 en `DECISIONES.md`).

## Tiempo

**[COMPLETAR POR EL AUTOR]** Horas reales invertidas (total y, si se quiere, por bloque):

| Bloque | Horas reales |
|---|---|
| Fase 1 (diseño) | [COMPLETAR POR EL AUTOR] |
| Bloques 1-2 (recepción, normalización y cola) | [COMPLETAR POR EL AUTOR] |
| Bloque 3 (API de pacientes) | [COMPLETAR POR EL AUTOR] |
| Bloque 4 (clasificación) | [COMPLETAR POR EL AUTOR] |
| Bloque 5 (enrutado, avisos, errores y n8n) | [COMPLETAR POR EL AUTOR] |
| Bloque 6 (cierre) | [COMPLETAR POR EL AUTOR] |
| **Total** | [COMPLETAR POR EL AUTOR] |
