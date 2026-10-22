# canvas-notion-sync

Sincroniza tareas, quizzes, discusiones, eventos, anuncios, recursos de módulos y notas de Canvas Instructure hacia una base de datos de Notion, corriendo automáticamente cada 3 horas vía GitHub Actions.

Guía completa: ver el post "Canvas + Notion: guía de sincronización" en Notion.

## Setup

1. **Fork / clona este repo.**
2. En GitHub, ve a **Settings → Secrets and variables → Actions** y agrega estos 4 secrets:
   - `CANVAS_DOMAIN` — ej. `tuescuela.instructure.com` (sin `https://`)
   - `CANVAS_TOKEN` — tu Canvas Access Token (Account → Settings → New Access Token)
   - `NOTION_TOKEN` — el Internal Integration Secret de tu integración de Notion
   - `NOTION_DATABASE_ID` — el ID de tu base de datos "Canvas Sync" en Notion
   - `ARCHIVE_OVERDUE_AFTER_DAYS` (opcional) — días de gracia antes de archivar tareas vencidas sin completar. Default: `30`.
   - `ANNOUNCEMENTS_LOOKBACK_DAYS` (opcional) — cuántos días atrás de anuncios traer en cada corrida. Default: `30`.
   - `TIMEZONE` (opcional) — zona horaria para las fechas que aparecen dentro del contenido de cada página (ej. `America/El_Salvador`). Default: `UTC`.
3. Confirma que la base de datos de Notion tenga estas propiedades: `Name` (title), `Course` (select), `Type` (select), `Due Date` (date), `Status` (status), `Canvas Link` (url), `Canvas ID` (rich text), `Grade` (rich text).
   - `Type` va a ir recibiendo valores nuevos automáticamente (Notion los crea solo): `Assignment`, `Quiz`, `Discussion Topic`, `Calendar Event`, `Announcement`, `File`, `Page`, `External Url`, `External Tool`, `Course Grade`.
4. Confirma que tu integración de Notion esté conectada a esa base de datos (`···` → Connections).
   - La integración necesita las capabilities de contenido (leer, insertar y actualizar contenido), que vienen activas por defecto en una integración interna.
5. Ve a la pestaña **Actions** del repo y corre el workflow "Sync Canvas to Notion" manualmente (`Run workflow`) para probarlo.

## Botón "Sincronizar ahora" en Notion (opcional)

Para forzar un sync sin esperar las 3 horas. Notion no puede llamar la API de GitHub directamente, así que el botón manda un webhook a un relay en Vercel ([api/trigger.py](api/trigger.py)) que valida un secreto y dispara el workflow con `workflow_dispatch`.

```
Botón de Notion ──POST + X-Trigger-Secret──▶ Vercel /api/trigger ──workflow_dispatch──▶ GitHub Actions ──▶ sync.py
```

### Setup

1. **Token de GitHub**: crea un *fine-grained token* (Settings → Developer settings → Fine-grained tokens) solo para este repo, con el permiso **Actions: Read and write**. Ponle fecha de expiración.
2. **Secreto del botón**: genera uno con `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. **Vercel**: importa el repo como proyecto con *Framework Preset* = **Other** (si queda en "Python" el build falla con `PYTHON_ENTRYPOINT_NOT_FOUND`). Agrega en Settings → Environment Variables, solo para **Production** y como *Sensitive*:
   - `GITHUB_TOKEN` — el token del paso 1
   - `TRIGGER_SECRET` — el secreto del paso 2
   - `GITHUB_REPO` (opcional) — `owner/repo`. Default: `SantiMerino/canvas-notion-sync`.
   - `GITHUB_REF` (opcional) — rama a correr. Default: `master`.

   Después de cambiar variables hay que redesplegar (`vercel deploy --prod --yes` o *Redeploy* en el dashboard). [.vercelignore](.vercelignore) hace que solo se publique `api/`.
4. **Notion**: agrega un bloque `/button` (ej. "🔄 Sincronizar ahora") con la acción **Send webhook**:
   - URL: `https://<tu-proyecto>.vercel.app/api/trigger`
   - Custom header: `X-Trigger-Secret` = el secreto del paso 2

### Uso y verificación

Al pulsar el botón el sync queda en cola en GitHub Actions y Notion se actualiza en ~2–3 min. Notion no muestra si el webhook funcionó; para confirmarlo:

- En **Actions** debe aparecer una corrida nueva con evento `workflow_dispatch` (las automáticas dicen `schedule`), o desde la terminal: `gh run list --workflow sync.yml --limit 3`.
- Si no aparece, revisa los logs del proyecto en Vercel (`POST /api/trigger`): `401` = el header no coincide con `TRIGGER_SECRET`; `429` = llegaste al límite diario o al tiempo mínimo entre syncs (ver [Límites](#límites)); `403` = lo bloqueó el Firewall; `502` = falla el token de GitHub (permisos o expiró).
- Prueba manual (en PowerShell usa `curl.exe`, no `curl`):
  ```bash
  curl.exe -X POST https://<tu-proyecto>.vercel.app/api/trigger -H "X-Trigger-Secret: <secreto>"
  ```

### Límites

El cron corre 8 veces al día (cada 3 h); el botón agrega como máximo otras 8, o sea un sync cada ~1.5 h en promedio.

| Límite | Default | Respuesta | Variable en Vercel |
|---|---|---|---|
| Syncs manuales en las últimas 24 h | 8 | `429` | `TRIGGER_DAILY_LIMIT` |
| Tiempo mínimo entre syncs manuales | 10 min | `429` | `TRIGGER_COOLDOWN_MINUTES` |

El contador es el propio historial de GitHub Actions (corridas `workflow_dispatch`), así que no hace falta base de datos; las corridas lanzadas a mano desde GitHub también cuentan. Si GitHub no responde, el relay no dispara (falla cerrado). Además el workflow tiene `concurrency`: como máximo una corrida activa y una en espera.

### Seguridad

Capas, de afuera hacia adentro:

1. **Vercel Firewall** (Settings → Firewall del proyecto), antes de que se ejecute la función:
   - *Trigger: solo POST con header* — `403` a todo lo que llegue a `/api/trigger` sin ser `POST` o sin el header `X-Trigger-Secret`.
   - *Trigger: rate limit por IP* — más de 10 llamadas por hora desde una IP reciben `403` durante el resto de esa hora.
2. **Secreto** — sin el valor exacto de `TRIGGER_SECRET` responde `401` (comparación en tiempo constante). El body del webhook se ignora.
3. **Cuota** — aun con el secreto, máximo `TRIGGER_DAILY_LIMIT` syncs por 24 h (ver arriba), así que un secreto filtrado no sirve para abusar de Canvas/Notion.
4. **Alcance del token** — el PAT solo tiene Actions sobre este repo, y el relay solo puede disparar `sync.yml` en `GITHUB_REF`. Las variables son *Sensitive* y solo existen en Production; los previews (incluidos PRs de forks) no las reciben y piden login de Vercel.

Cualquiera con permiso de edición sobre la página del botón puede ver el header, así que mantén esa página privada. Si el secreto se filtra, genera otro, cámbialo en Vercel y en el botón, y redespliega.

## Estado del sync en Notion (opcional)

Cada corrida (cron o botón) escribe su resultado en una fila `Canvas → Notion` de una base aparte, **Estado de sincronización** ([sync_status.py](sync_status.py)), separada de `Canvas Sync`. Así sabes cuándo terminó el sync que lanzaste con el botón y si encontró cambios.

| Propiedad | Tipo | Qué guarda |
|---|---|---|
| `Proceso` | title | `Canvas → Notion` (la fila se crea sola si no existe) |
| `Estado` | select | `En ejecución`, `Éxito con cambios`, `Éxito sin cambios`, `Completado con avisos`, `Error` |
| `Último intento` / `Última finalización` | date | inicio y fin de la corrida |
| `Nuevos` / `Actualizados` | number | páginas creadas / con propiedades o contenido cambiado |
| `Resultado` | text | resumen: nuevos · actualizados · archivados · avisos (o el tipo de error) |
| `Corrida GitHub` | url | link a la corrida de Actions |

Setup:

1. Crea la base con esas propiedades y conecta a ella la misma integración de Notion del sync (`···` → Connections).
2. En GitHub, **Settings → Secrets and variables → Actions → Variables**, agrega `SYNC_STATUS_DATABASE_ID` con el ID de la base (es un ID, no un secreto). Sin esta variable el reporte simplemente no corre.

El reporte es best-effort: si Notion falla o la integración no tiene acceso, el sync sigue igual y el log muestra un aviso. `sync.py` reporta su propio final (incluido `Error` si se cae); si el job falla antes (ej. `pip install`) o se cancela, el último paso del workflow marca `Error`.

## Logs públicos

El repo es público, así que sus logs de Actions también. El script imprime solo conteos (items encontrados, nuevos, actualizados, avisos) y nunca nombres de cursos, tareas o archivos. Los errores se registran solo con su tipo y código HTTP, porque el texto de algunos errores de Canvas incluye URLs de descarga con tokens.

## Correr localmente (opcional)

```bash
pip install -r requirements.txt
export CANVAS_DOMAIN="tuescuela.instructure.com"
export CANVAS_TOKEN="..."
export NOTION_TOKEN="..."
export NOTION_DATABASE_ID="..."
python sync.py
```

## Cómo funciona

- Llama a `GET /api/v1/planner/items` de Canvas, que agrupa tareas, quizzes, discusiones con fecha y eventos de calendario en una sola respuesta paginada.
- Por cada item, hace upsert en Notion usando `Canvas ID` (`plannable_type-plannable_id`) como llave para evitar duplicados en corridas repetidas.
- También trae los cursos activos y sus anuncios (`GET /api/v1/announcements`, últimos `ANNOUNCEMENTS_LOOKBACK_DAYS` días) y los sincroniza como items con `Type = Announcement`.
- Recorre los módulos de cada curso (`GET /api/v1/courses/:id/modules` + `.../items`) y sincroniza los recursos de contenido (archivos, páginas, enlaces/herramientas externas) con `Type = File/Page/External Url/External Tool`. Los items de módulo que son en realidad tareas/quizzes/discusiones se ignoran acá porque ya llegan por el planner.
- Trae las notas: por actividad (`GET /api/v1/courses/:id/assignments?include[]=submission`, propia entrega del estudiante) se guardan en la propiedad `Grade` del item de esa tarea; y la nota general por materia (`GET /api/v1/users/self/enrollments`) se sincroniza como una fila aparte por curso con `Type = Course Grade`, que se actualiza en el lugar en cada corrida.
- Escribe el contenido de cada página en una sección **📌 Detalles de Canvas** al inicio, para no tener que abrir Canvas:
  - Tareas, quizzes y discusiones: puntos, tipo de entrega, formatos permitidos, intentos, fechas de apertura/cierre, estado de tu entrega, instrucciones completas y rúbrica (como tabla).
  - Anuncios: el mensaje completo. Páginas de módulos: el contenido de la página.
  - El HTML de Canvas se convierte a markdown de Notion ([notion_markdown.py](notion_markdown.py)) y se escribe con `PATCH /v1/pages/:id/markdown` (API `2026-03-11`). Las ecuaciones del editor de Canvas quedan como ecuaciones de Notion; las imágenes quedan como link porque requieren sesión de Canvas.
  - Debajo queda un encabezado **✍️ Mis notas**: todo lo que escribas ahí no se toca. La sección de Canvas se reemplaza solo cuando cambia algo en Canvas (lleva un `ref:` con un hash en su pie), así que no edites dentro de ella.
- Propiedades extra para priorizar (se crean solas si faltan): `Puntos`, `Entrega` (Sin entregar / Entregado / Tarde / Calificado / Faltante / No aplica), `Nota %`, y las fórmulas `Días restantes` y `Urgencia` (⚫ Vencida / 🔴 Alta ≤48 h / 🟠 Media ≤7 días / 🟢 Baja). Si Canvas dice que ya entregaste, el item pasa a `Done`.
- `Resumen`, `Esfuerzo (h)` y `Revisar con IA` no las escribe el script: son para Notion AI (autofill) y los Custom Agents.
- Los PDFs de los módulos se copian dentro de su página de Notion (File Upload API, una sola vez por archivo) para que Notion AI los pueda leer.
- Solo se escriben las propiedades que cambiaron, para no marcar todas las páginas como editadas en cada corrida.
- Al final de cada corrida, archiva (Notion `archived: true`, recuperable desde la papelera) las tareas vencidas: de inmediato si ya están en `Done`, o después de `ARCHIVE_OVERDUE_AFTER_DAYS` días si nunca se marcaron como completadas. Anuncios, recursos y notas generales no tienen `Due Date`, así que nunca se archivan solos.
- Sync bidireccional parcial, limitado al flag de "completado" (nunca toca entregas ni calificaciones):
  - Canvas → Notion: si el item está marcado como hecho en el To-Do de Canvas (`planner_override.marked_complete`), se pone `Status = Done` en Notion.
  - Notion → Canvas: si el estudiante pone `Status = Done` en Notion antes de que Canvas lo sepa, se crea/actualiza un `planner_override` en Canvas con `marked_complete: true` vía `POST/PUT /api/v1/planner/overrides`.
  - Fuera de ese flag, el resto de los campos siguen siendo de un solo sentido: Canvas → Notion. Las notas son de solo lectura (Canvas → Notion), nunca se escriben calificaciones de vuelta.
