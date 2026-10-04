# Canvas Sync: Notion export (Education workspace)

Snapshot taken on 2026-10-04 from the Education-plan workspace, so the database can be recreated in the Business-plan workspace before applying the improvements.

## 1. Database

| Field | Value |
|---|---|
| Name | **Canvas Sync** |
| Kind | Full-page database, at the workspace's top level (no parent page) |
| Icon | Custom emoji `canvas-lms`. Custom emojis don't move between workspaces: re-upload it in the new one ([image](https://s3-us-west-2.amazonaws.com/public.notion-static.com/0463d9f4-0eda-4613-b8c5-d05179b91a75/image.png)) |
| Data sources | 1 (`Canvas Sync`). The script always uses `data_sources[0]` |
| Old database ID | `f3a22507-2977-4b1f-b81b-4f007dcbc396` (the `NOTION_DATABASE_ID` secret) |
| Old data source ID | `8463ae95-aa17-4ac8-9179-8b50f43452c9` |

## 2. Properties

The names have to match exactly, because `sync.py` writes to them by name.

| Property | Type | Written by the sync | Notes |
|---|---|---|---|
| `Name` | Title | Yes | Canvas title. Course grades use `Nota general: <course>` |
| `Course` | Select | Yes | Course name (`context_name`). New options are created automatically |
| `Type` | Select | Yes | Item kind (see the options below) |
| `Due Date` | Date | Yes | `plannable_date` for planner items, `posted_at` for announcements. Empty for resources and grades |
| `Status` | Status | Yes, two-way | Only `Done` syncs with Canvas (`planner_override.marked_complete`) |
| `Canvas Link` | URL | Yes | Always an absolute URL |
| `Canvas ID` | Text | Yes | Dedup key. Description: *"ID unico de Canvas, usado por el script para evitar duplicados (plannable_type-plannable_id)"*. Formats: `assignment-123`, `quiz-123`, `announcement-123`, `module_item-123`, `course_grade-123` |
| `Grade` | Text | Yes | `score/points (letter)` per activity. `xx.x% (letter)` for course grades |
| `Place` | Place | No | Not used by the script. It can be left out of the new database |

### `Status` options

| Group | Option | Color |
|---|---|---|
| To-do | Not started | default |
| In progress | In progress | blue |
| Complete | Done | green |

### `Type` options (color)

Current options: Assignment (blue), Quiz (purple), Discussion (green), Event (orange), Announcement (red), Page (gray), External Url (brown), File (default), Course Grade (pink).

Values the script can write, which Notion creates automatically if they're missing: `Assignment`, `Quiz`, `Discussion Topic`, `Calendar Event`, `Planner Note`, `Wiki Page`, `Announcement`, `File`, `Page`, `External Url`, `External Tool`, `Course Grade`.

> `Discussion` and `Event` were never written by the script. It writes `Discussion Topic` and `Calendar Event`. In the new database, create those exact names so the colors and filters line up.

### `Course` options (color)

| Option | Color | View emoji |
|---|---|---|
| General | gray | (fallback when Canvas sends no course) |
| Matemáticas Multivariable y Ecuaciones Diferenciales | default | 📐 |
| Colaboración Efectiva | blue | 🤝 |
| Probabilidad Estadística y Machine Learning | pink | 📊 |
| Física Aplicada III | yellow | ⚛️ |
| Estructura de Datos y Algoritmos Computacionales | brown | 🌳 |
| Key Institute | purple | |

## 3. Views (11, in order)

| # | Name | Type | Filter | Sort | Visible properties / grouping |
|---|---|---|---|---|---|
| 1 | 📐 Multivariable | Table | Type = Assignment **and** Course = Matemáticas Multivariable… | Due Date ↑ | Name, Due Date, Status, Grade, Canvas Link |
| 2 | 🤝 Colaboración | Table | Type = Assignment **and** Course = Colaboración Efectiva | Due Date ↑ | Name, Due Date, Status, Grade, Canvas Link |
| 3 | ⚛️ Física III | Table | Type = Assignment **and** Course = Física Aplicada III | Due Date ↑ | Name, Due Date, Status, Grade, Canvas Link |
| 4 | 🌳 EDA | Table | Type = Assignment **and** Course = Estructura de Datos… | Due Date ↑ | Name, Due Date, Status, Grade, Canvas Link |
| 5 | 📊 PEML | Table | Type = Assignment **and** Course = Probabilidad Estadística… | Due Date ↑ | Name, Due Date, Status, Grade, Canvas Link |
| 6 | Calendar | Calendar (by Due Date) | — | — | Name, Canvas Link, Course |
| 7 | Notas | Table | Type = Course Grade (+ an empty Course filter, a leftover) | Due Date ↑ | Name, Grade, Canvas ID, Canvas Link, Course, Type |
| 8 | *(untitled)* | Board | — | — | Grouped by Course (empty groups hidden). Card: Name |
| 9 | Gallery | Gallery | — | — | Grouped by Course (empty groups hidden). Compact card, page-content preview. Card: Name |
| 10 | ⏳ Pending | Table | Type = Assignment **and** Status ≠ Done **and** Grade is empty **and** Due Date not empty | Due Date ↑ | Name, Course, Due Date, Status, Canvas Link |
| 11 | Assigments | Table | Type ∈ {Assignment, File} **and** Course = Matemáticas Multivariable… | Due Date ↑ | All properties |

> To clean up while recreating:
> - View 7 has an empty Course filter.
> - View 8 has no name.
> - View 11 ("Assigments", with a typo) looks like an unfinished duplicate of view 1.
> - The per-course views only show `Assignment`, so quizzes and discussions are left out of them.

## 4. Content (rows)

178 rows, all generated by the sync. No row has a `Status` other than `Not started`, and the sync never wrote page content (a sampled page was empty). **No manual data is lost**: the first run in the new workspace repopulates everything. If you wrote notes inside any page, copy them by hand.

| Type | Rows | Breakdown by course |
|---|---|---|
| Page | 51 | Colaboración 43 · EDA 7 · Multivariable 1 |
| File | 81 | Multivariable 29 · Física III 27 · PEML 20 · Key Institute 5 |
| External Url | 18 | PEML 14 · Multivariable 3 · EDA 1 |
| Assignment | 14 | Física III 7 · Colaboración 4 (2 graded) · Multivariable 2 · EDA 1 |
| Announcement | 9 | Colaboración 5 · 1 each in the other 4 courses |
| Course Grade | 5 | One per course, except Key Institute |

## 5. Related pages (outside the database)

These are blog posts under `web-blog`. They're not needed for the sync to work:
- *Canvas + Notion parte 1: guía de sincronización* (`3bfefd10-7f31-810b-bcdc-ceb9c91037ee`)
- *Canvas + Notion parte 2: features avanzados (planteamiento)* (`3c0efd10-7f31-8153-9153-e75abc370cb5`)

## 6. Migrating to the Business workspace

1. In the Business workspace, create an internal integration (Settings → Connections → Develop or manage integrations). Leave the content capabilities enabled: read, insert and update content.
2. Create the **Canvas Sync** database with the properties from section 2. `Status` has to be of type *Status* with exactly the options `Not started` / `In progress` / `Done`.
3. Connect the integration to the database (`···` → Connections).
4. Update these secrets in GitHub:
   - `NOTION_TOKEN`: the new integration's token.
   - `NOTION_DATABASE_ID`: the new database's ID.
   - Optional: `TIMEZONE` (e.g. `America/El_Salvador`).
5. Run the "Sync Canvas to Notion" workflow manually. The first run fills the rows and writes each page's **📌 Detalles de Canvas** section.
6. Recreate the views from section 3, keeping the cleanups noted there.
7. Once you've checked it works, revoke the old integration in the Education workspace so two syncs don't run in parallel.
