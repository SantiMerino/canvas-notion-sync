import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from notion_client import APIResponseError, Client

from notion_markdown import escape_text, html_to_notion_markdown

CANVAS_DOMAIN = os.environ["CANVAS_DOMAIN"]  # ej. tuescuela.instructure.com
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

# Tareas vencidas y sin completar se archivan solas pasado este tiempo, para
# que no se acumulen indefinidamente. Configurable vía secret opcional.
ARCHIVE_OVERDUE_AFTER_DAYS = int(os.environ.get("ARCHIVE_OVERDUE_AFTER_DAYS") or "30")

# Cuántos días atrás de anuncios traer en cada corrida. Los anuncios no
# cambian una vez posteados, así que no hace falta mirar más atrás de esto.
ANNOUNCEMENTS_LOOKBACK_DAYS = int(os.environ.get("ANNOUNCEMENTS_LOOKBACK_DAYS") or "30")

# Zona horaria para mostrar fechas dentro del contenido de cada página
# (ej. "America/El_Salvador"). Las propiedades de fecha no dependen de esto.
TIMEZONE = os.environ.get("TIMEZONE") or "UTC"

CANVAS_HEADERS = {"Authorization": f"Bearer {CANVAS_TOKEN}"}

# El contenido de cada página tiene una sección que maneja el script (entre
# estos dos encabezados) y el resto queda libre para las notas del estudiante.
# La sección se reescribe solo cuando cambia lo que viene de Canvas, detectado
# con el hash que va en su pie ("ref:...").
DETAILS_HEADING = "## 📌 Detalles de Canvas"
NOTES_HEADING = "## ✍️ Mis notas"
DETAILS_FOOTER = "Se actualiza solo desde Canvas en cada sync; no edites esta sección."
DETAILS_REF = re.compile(r"ref:([0-9a-f]{10})")

SUBMISSION_TYPE_LABELS = {
    "online_upload": "Subir archivo",
    "online_text_entry": "Texto en línea",
    "online_url": "URL",
    "media_recording": "Grabación de audio/video",
    "student_annotation": "Anotación en documento",
    "online_quiz": "Quiz",
    "discussion_topic": "Discusión",
    "external_tool": "Herramienta externa",
    "on_paper": "En papel",
    "none": "Sin entrega en Canvas",
}

SUBMISSION_STATE_LABELS = {
    "unsubmitted": "Sin entregar",
    "submitted": "Entregado",
    "pending_review": "En revisión",
    "graded": "Calificado",
}

# Tipos de item de módulo que son recursos de contenido (material subido por
# el profesor). El resto (Assignment, Quiz, Discussion, SubHeader) ya llega
# por el planner o no tiene contenido propio, así que se ignoran acá.
MODULE_RESOURCE_TYPE_LABELS = {
    "File": "File",
    "Page": "Page",
    "ExternalUrl": "External Url",
    "ExternalTool": "External Tool",
}

notion = Client(auth=NOTION_TOKEN)
# Los endpoints de markdown (/v1/pages/:id/markdown) solo existen desde esta
# versión de la API; el resto del script se queda en la versión por defecto.
notion_md = Client(auth=NOTION_TOKEN, notion_version="2026-03-11")


def get_data_source_id():
    # Notion's API separates a "database" from its "data source" (a database
    # can have multiple sources). Queries/creates go through the data source.
    database = notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
    return database["data_sources"][0]["id"]


# Propiedades que el script necesita en la base. Si falta alguna (ej. una base
# recién creada que solo tiene "Name"), se crea al inicio de cada corrida; las
# opciones de los selects las va agregando Notion solo a medida que llegan.
REQUIRED_PROPERTIES = {
    "Course": {"select": {}},
    "Type": {"select": {}},
    "Due Date": {"date": {}},
    "Status": {"status": {}},
    "Canvas Link": {"url": {}},
    "Canvas ID": {"rich_text": {}},
    "Grade": {"rich_text": {}},
    "Puntos": {"number": {}},
    "Nota %": {"number": {}},
    "Entrega": {
        "select": {
            "options": [
                {"name": "Sin entregar", "color": "gray"},
                {"name": "Entregado", "color": "blue"},
                {"name": "Tarde", "color": "orange"},
                {"name": "Calificado", "color": "green"},
                {"name": "Faltante", "color": "red"},
                {"name": "No aplica", "color": "default"},
            ]
        }
    },
    # Las llena Notion AI (autofill) y los Custom Agents, no el script.
    "Resumen": {"rich_text": {}},
    "Esfuerzo (h)": {"number": {}},
    "Revisar con IA": {"checkbox": {}},
}

TASK_TYPES_FORMULA = (
    '(prop("Type") == "Assignment" or prop("Type") == "Quiz" or prop("Type") == "Discussion Topic")'
)

# Fórmulas: se calculan solas en Notion (gratis y siempre al día), por eso no
# las escribe el script. Van después del resto porque dependen de "Entrega".
FORMULA_PROPERTIES = {
    "Días restantes": {"formula": {"expression": 'dateBetween(prop("Due Date"), now(), "days")'}},
    "Urgencia": {
        "formula": {
            "expression": (
                f'if(not {TASK_TYPES_FORMULA} or empty(prop("Due Date")) or prop("Status") == "Done"'
                ' or prop("Entrega") == "Entregado" or prop("Entrega") == "Calificado"'
                ' or prop("Entrega") == "Tarde", "",'
                ' if(dateBetween(prop("Due Date"), now(), "hours") < 0, "⚫ Vencida",'
                ' if(dateBetween(prop("Due Date"), now(), "hours") <= 48, "🔴 Alta",'
                ' if(dateBetween(prop("Due Date"), now(), "hours") <= 168, "🟠 Media", "🟢 Baja"))))'
            )
        }
    },
}


def ensure_schema(data_source_id):
    data_source = notion.data_sources.retrieve(data_source_id=data_source_id)
    existing = data_source.get("properties") or {}
    for group in (REQUIRED_PROPERTIES, FORMULA_PROPERTIES):
        missing = {name: config for name, config in group.items() if name not in existing}
        if not missing:
            continue
        try:
            notion.data_sources.update(data_source_id=data_source_id, properties=missing)
            print(f"Propiedades creadas en Notion: {', '.join(missing)}")
        except APIResponseError as error:
            if group is REQUIRED_PROPERTIES:
                raise
            # Una fórmula inválida no debería frenar el sync.
            print(f"  ⚠️ No se pudieron crear las fórmulas {', '.join(missing)}: {error}")


def canvas_get_paginated(url, params=None):
    items = []
    while url:
        resp = requests.get(url, headers=CANVAS_HEADERS, params=params)
        resp.raise_for_status()
        items.extend(resp.json())
        # Canvas pagina con un header Link estilo GitHub
        url = resp.links.get("next", {}).get("url")
        params = None
    return items


def fetch_planner_items():
    url = f"https://{CANVAS_DOMAIN}/api/v1/planner/items"
    return canvas_get_paginated(url, {"per_page": 50})


def fetch_active_courses():
    url = f"https://{CANVAS_DOMAIN}/api/v1/courses"
    return canvas_get_paginated(url, {"enrollment_state": "active", "per_page": 50})


def fetch_announcements(course_ids):
    if not course_ids:
        return []
    url = f"https://{CANVAS_DOMAIN}/api/v1/announcements"
    start_date = (
        datetime.now(timezone.utc).date() - timedelta(days=ANNOUNCEMENTS_LOOKBACK_DAYS)
    ).isoformat()
    params = {
        "context_codes[]": [f"course_{course_id}" for course_id in course_ids],
        "start_date": start_date,
        "per_page": 50,
    }
    return canvas_get_paginated(url, params)


def fetch_module_resources(course_ids):
    resources = []
    for course_id in course_ids:
        modules_url = f"https://{CANVAS_DOMAIN}/api/v1/courses/{course_id}/modules"
        modules = canvas_get_paginated(modules_url, {"per_page": 50})
        for module in modules:
            items_url = (
                f"https://{CANVAS_DOMAIN}/api/v1/courses/{course_id}"
                f"/modules/{module['id']}/items"
            )
            items = canvas_get_paginated(items_url, {"per_page": 50})
            for item in items:
                if item.get("type") in MODULE_RESOURCE_TYPE_LABELS:
                    resources.append((course_id, item))
    return resources


def fetch_assignments(course_ids):
    # Todas las tareas de los cursos activos con la propia entrega del
    # estudiante (no requiere acceso de profesor/gradebook). Trae descripción,
    # rúbrica y reglas de entrega para el contenido de la página, y la nota.
    # Se indexan por el tipo/id con el que aparecen en el planner: los quizzes
    # y discusiones calificadas también tienen una tarea asociada.
    index = {}
    for course_id in course_ids:
        url = f"https://{CANVAS_DOMAIN}/api/v1/courses/{course_id}/assignments"
        assignments = canvas_get_paginated(url, {"per_page": 50, "include[]": "submission"})
        for assignment in assignments:
            index[("assignment", assignment["id"])] = assignment
            if assignment.get("quiz_id"):
                index[("quiz", assignment["quiz_id"])] = assignment
            topic = assignment.get("discussion_topic") or {}
            if topic.get("id"):
                index[("discussion_topic", topic["id"])] = assignment
    return index


def canvas_get_optional(url):
    # Para contenido que puede estar bloqueado para el estudiante (módulos aún
    # no liberados, quizzes ocultos): si Canvas lo niega, se sigue sin él.
    resp = requests.get(url, headers=CANVAS_HEADERS)
    if resp.status_code in (401, 403, 404):
        return None
    resp.raise_for_status()
    return resp.json()


def fetch_course_grades():
    # nota general de cada materia, vía las propias inscripciones del estudiante.
    url = f"https://{CANVAS_DOMAIN}/api/v1/users/self/enrollments"
    params = {"per_page": 50, "state[]": "active", "type[]": "StudentEnrollment"}
    enrollments = canvas_get_paginated(url, params)
    return {enrollment["course_id"]: (enrollment.get("grades") or {}) for enrollment in enrollments}


def format_grade(score, points_possible, letter):
    text = f"{score}/{points_possible}" if points_possible else str(score)
    if letter and str(letter) != str(score):
        text += f" ({letter})"
    return text


def find_existing_page(data_source_id, canvas_id):
    result = notion.data_sources.query(
        data_source_id=data_source_id,
        filter={"property": "Canvas ID", "rich_text": {"equals": str(canvas_id)}},
    )
    return result["results"][0] if result["results"] else None


def get_notion_status(page):
    status = (page.get("properties", {}).get("Status") or {}).get("status")
    return status["name"] if status else None


def property_value(prop):
    # Normaliza una propiedad (tal como la devuelve Notion o como la mandamos)
    # a un valor comparable.
    if not prop:
        return None
    for kind in ("title", "rich_text"):
        if kind in prop:
            return "".join(
                part.get("plain_text") or part.get("text", {}).get("content", "")
                for part in prop[kind] or []
            )
    for kind in ("select", "status"):
        if kind in prop:
            return (prop[kind] or {}).get("name")
    if "date" in prop:
        start = (prop["date"] or {}).get("start")
        return datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None
    for kind in ("url", "number", "checkbox"):
        if kind in prop:
            return prop[kind]
    return None


def update_page(page, properties):
    # Solo escribe lo que cambió. Reescribir todo en cada corrida gasta llamadas
    # a la API y marca cada página como editada cada 3 horas.
    current = page.get("properties", {})
    changed = {
        name: value
        for name, value in properties.items()
        if property_value(value) != property_value(current.get(name))
    }
    if changed:
        notion.pages.update(page_id=page["id"], properties=changed)
    return bool(changed)


def submission_label(assignment):
    types = set(assignment.get("submission_types") or [])
    if not types or types <= {"none", "on_paper"}:
        return "No aplica"
    submission = assignment.get("submission") or {}
    state = submission.get("workflow_state")
    if submission.get("missing"):
        return "Faltante"
    if submission.get("submitted_at"):
        if submission.get("late"):
            return "Tarde"
        return "Calificado" if state == "graded" else "Entregado"
    if state == "graded":
        return "Calificado"
    return "Sin entregar"


NOTION_SINGLE_PART_LIMIT = 20 * 1024 * 1024
NOTION_PART_SIZE = 10 * 1024 * 1024


def upload_to_notion(filename, content, content_type):
    if len(content) <= NOTION_SINGLE_PART_LIMIT:
        upload = notion.file_uploads.create(
            mode="single_part", filename=filename, content_type=content_type
        )
        notion.file_uploads.send(file_upload_id=upload["id"], file=(filename, content, content_type))
        return upload["id"]

    parts = [content[i : i + NOTION_PART_SIZE] for i in range(0, len(content), NOTION_PART_SIZE)]
    upload = notion.file_uploads.create(
        mode="multi_part", filename=filename, content_type=content_type, number_of_parts=len(parts)
    )
    for number, part in enumerate(parts, start=1):
        notion.file_uploads.send(
            file_upload_id=upload["id"], file=(filename, part, content_type), part_number=str(number)
        )
    notion.file_uploads.complete(file_upload_id=upload["id"])
    return upload["id"]


def sync_pdf(page_id, item):
    # Copia el PDF de Canvas dentro de la página de Notion para que Notion AI
    # pueda leerlo. Se sube una sola vez: si la página ya tiene un PDF, no se toca.
    try:
        children = notion.blocks.children.list(block_id=page_id, page_size=50)["results"]
        if any(block["type"] == "pdf" for block in children):
            return
        file_info = canvas_get_optional(item["url"])
        if not file_info or file_info.get("content-type") != "application/pdf" or not file_info.get("url"):
            return
        resp = requests.get(file_info["url"], headers=CANVAS_HEADERS)
        resp.raise_for_status()
        filename = file_info.get("display_name") or file_info.get("filename") or "archivo.pdf"
        upload_id = upload_to_notion(filename, resp.content, "application/pdf")

        blocks = [{"type": "pdf", "pdf": {"type": "file_upload", "file_upload": {"id": upload_id}}}]
        if not children:
            blocks.append(
                {
                    "type": "heading_2",
                    "heading_2": {"rich_text": [{"type": "text", "text": {"content": "✍️ Mis notas"}}]},
                }
            )
        notion.blocks.children.append(block_id=page_id, children=blocks)
        print(f"  PDF copiado a Notion: {filename}")
    except (APIResponseError, requests.RequestException) as error:
        print(f"  ⚠️ No se pudo copiar el PDF a {page_id}: {error}")


def build_canvas_link(html_url):
    # La Planner API a veces devuelve una ruta relativa (ej. "/courses/297/assignments/2447")
    # en vez de una URL absoluta. Sin el dominio, Notion no la reconoce como link clickeable.
    if not html_url:
        return None
    if html_url.startswith("http://") or html_url.startswith("https://"):
        return html_url
    return f"https://{CANVAS_DOMAIN}{html_url}"


def local_timezone():
    try:
        return ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        return timezone.utc


def date_mention(iso_datetime):
    # Fecha clickeable de Notion (se puede usar para recordatorios).
    tz = local_timezone()
    moment = datetime.fromisoformat(iso_datetime.replace("Z", "+00:00")).astimezone(tz)
    return (
        f'<mention-date start="{moment:%Y-%m-%d}" startTime="{moment:%H:%M}" '
        f'timeZone="{getattr(tz, "key", "UTC")}"/>'
    )


def format_number(value):
    return f"{value:g}" if isinstance(value, float) else str(value)


def submission_summary(submission):
    state = submission.get("workflow_state")
    if not state:
        return None
    text = SUBMISSION_STATE_LABELS.get(state, state)
    if submission.get("submitted_at"):
        text += f" el {date_mention(submission['submitted_at'])}"
    if submission.get("attempt"):
        text += f" (intento {submission['attempt']})"
    if submission.get("late"):
        text += " · **tarde**"
    if submission.get("missing"):
        text += " · **faltante**"
    return text


def rubric_markdown(rubric):
    rows = ["\t<tr>\n\t\t<td>Criterio</td>\n\t\t<td>Niveles</td>\n\t\t<td>Pts</td>\n\t</tr>"]
    for criterion in rubric:
        name = f"**{escape_text(criterion.get('description'))}**"
        if criterion.get("long_description"):
            name += f"<br>{escape_text(criterion['long_description'])}"
        levels = []
        for rating in criterion.get("ratings") or []:
            level = f"**{escape_text(rating.get('description'))}** ({format_number(rating.get('points'))})"
            if rating.get("long_description"):
                level += f": {escape_text(rating['long_description'])}"
            levels.append(level)
        rows.append(
            f"\t<tr>\n\t\t<td>{name}</td>\n\t\t<td>{'<br>'.join(levels)}</td>\n"
            f"\t\t<td>{format_number(criterion.get('points'))}</td>\n\t</tr>"
        )
    return '<table header-row="true">\n' + "\n".join(rows) + "\n</table>"


def details_markdown(description_html=None, facts=(), rubric=None, heading="Instrucciones"):
    base_url = f"https://{CANVAS_DOMAIN}"
    parts = []
    facts = [fact for fact in facts if fact]
    if facts:
        parts.append("\n".join(f"- {fact}" for fact in facts))
    description = html_to_notion_markdown(description_html, base_url)
    if description:
        parts.append(f"### {heading}\n{description}")
    if rubric:
        parts.append(f"### Rúbrica\n{rubric_markdown(rubric)}")
    return "\n\n".join(parts)


def assignment_facts(assignment):
    facts = []
    if assignment.get("points_possible") is not None:
        facts.append(f"**Puntos:** {format_number(assignment['points_possible'])}")
    types = [SUBMISSION_TYPE_LABELS.get(t, t) for t in assignment.get("submission_types") or []]
    if types:
        facts.append(f"**Tipo de entrega:** {', '.join(types)}")
    if assignment.get("allowed_extensions"):
        facts.append(f"**Formatos permitidos:** {escape_text(', '.join(assignment['allowed_extensions']))}")
    attempts = assignment.get("allowed_attempts")
    if attempts and attempts > 0:
        facts.append(f"**Intentos permitidos:** {attempts}")
    if assignment.get("unlock_at"):
        facts.append(f"**Disponible desde:** {date_mention(assignment['unlock_at'])}")
    if assignment.get("lock_at"):
        facts.append(f"**Cierra:** {date_mention(assignment['lock_at'])}")
    summary = submission_summary(assignment.get("submission") or {})
    if summary:
        facts.append(f"**Tu entrega:** {summary}")
    return facts


def planner_item_details(item, assignments):
    # Arma el contenido de la página para tareas, quizzes y discusiones.
    # Lo que tiene tarea asociada sale del índice ya descargado; quizzes y
    # discusiones sin calificar se piden aparte.
    ptype, pid = item["plannable_type"], item["plannable_id"]
    course_id = item.get("course_id")
    assignment = assignments.get((ptype, pid))
    facts = assignment_facts(assignment) if assignment else []
    description = assignment.get("description") if assignment else None
    rubric = assignment.get("rubric") if assignment else None

    if ptype == "quiz" and course_id:
        quiz = canvas_get_optional(f"https://{CANVAS_DOMAIN}/api/v1/courses/{course_id}/quizzes/{pid}")
        if quiz:
            if quiz.get("time_limit"):
                facts.append(f"**Tiempo límite:** {quiz['time_limit']} min")
            if quiz.get("question_count"):
                facts.append(f"**Preguntas:** {quiz['question_count']}")
            if not assignment and (quiz.get("allowed_attempts") or 0) > 0:
                facts.append(f"**Intentos permitidos:** {quiz['allowed_attempts']}")
            description = description or quiz.get("description")
    elif ptype == "discussion_topic" and not assignment and course_id:
        topic = canvas_get_optional(
            f"https://{CANVAS_DOMAIN}/api/v1/courses/{course_id}/discussion_topics/{pid}"
        )
        if topic:
            description = topic.get("message")

    if not (facts or description or rubric):
        return ""
    return details_markdown(description, facts, rubric)


def sync_page_details(page_id, details_md):
    # Escribe o actualiza la sección "Detalles de Canvas" del contenido de la
    # página sin tocar nada de lo que el estudiante escribió debajo.
    if not details_md:
        return
    ref = hashlib.sha1(details_md.encode("utf-8")).hexdigest()[:10]
    section = f"{DETAILS_HEADING}\n{details_md}\n\n*{DETAILS_FOOTER} · ref:{ref}*\n---"
    path = f"pages/{page_id}/markdown"

    try:
        current = notion_md.request(path=path, method="GET")["markdown"]
        start = current.find(DETAILS_HEADING)
        if start == -1:
            # Página nueva o de antes de esta feature: se agrega arriba de todo.
            body = {
                "type": "insert_content",
                "insert_content": {
                    "content": f"{section}\n{NOTES_HEADING}\n",
                    "position": {"type": "start"},
                },
            }
        else:
            end = current.find(NOTES_HEADING, start)
            if end == -1:
                print(f"  ⚠️ No se encontró '{NOTES_HEADING}' en {page_id}; no se actualizan los detalles")
                return
            old_section = current[start:end].rstrip("\n")
            match = DETAILS_REF.search(old_section)
            if match and match.group(1) == ref:
                return  # sin cambios en Canvas
            body = {
                "type": "update_content",
                "update_content": {
                    "content_updates": [{"old_str": old_section, "new_str": section}]
                },
            }
        notion_md.request(path=path, method="PATCH", body=body)
    except APIResponseError as error:
        # Que una página con contenido raro no tumbe el sync completo.
        print(f"  ⚠️ No se pudieron escribir los detalles en {page_id}: {error}")


def mark_canvas_complete(item):
    # Refleja un "Done" puesto en Notion de vuelta a Canvas, usando el mismo
    # mecanismo que el checkbox de "marcar como hecho" en el To-Do de Canvas.
    # Nunca toca la entrega/calificación real, solo este flag del planner.
    override = item.get("planner_override") or {}
    payload = {
        "plannable_type": item["plannable_type"],
        "plannable_id": item["plannable_id"],
        "marked_complete": True,
    }
    if override.get("id"):
        url = f"https://{CANVAS_DOMAIN}/api/v1/planner/overrides/{override['id']}"
        resp = requests.put(url, headers=CANVAS_HEADERS, json=payload)
    else:
        url = f"https://{CANVAS_DOMAIN}/api/v1/planner/overrides"
        resp = requests.post(url, headers=CANVAS_HEADERS, json=payload)
    resp.raise_for_status()


def upsert_item(data_source_id, item, assignments):
    plannable = item.get("plannable") or {}
    canvas_id = f'{item["plannable_type"]}-{item["plannable_id"]}'
    canvas_complete = bool((item.get("planner_override") or {}).get("marked_complete"))

    properties = {
        "Name": {"title": [{"text": {"content": plannable.get("title", "Sin título")}}]},
        "Type": {"select": {"name": item["plannable_type"].replace("_", " ").title()}},
        "Course": {"select": {"name": item.get("context_name") or "General"}},
        "Canvas ID": {"rich_text": [{"text": {"content": canvas_id}}]},
        "Canvas Link": {"url": build_canvas_link(item.get("html_url"))},
    }
    if item.get("plannable_date"):
        properties["Due Date"] = {"date": {"start": item["plannable_date"]}}

    # Tareas, quizzes y discusiones calificadas: la nota sale de la tarea asociada.
    assignment = assignments.get((item["plannable_type"], item["plannable_id"])) or {}
    submission = assignment.get("submission") or {}
    if submission.get("score") is not None:
        properties["Grade"] = {
            "rich_text": [
                {
                    "text": {
                        "content": format_grade(
                            submission["score"],
                            assignment.get("points_possible"),
                            submission.get("grade"),
                        )
                    }
                }
            ]
        }
        if assignment.get("points_possible"):
            percent = submission["score"] / assignment["points_possible"] * 100
            properties["Nota %"] = {"number": round(percent, 2)}

    delivered = False
    if assignment:
        label = submission_label(assignment)
        properties["Entrega"] = {"select": {"name": label}}
        delivered = label in ("Entregado", "Tarde", "Calificado")
        if assignment.get("points_possible") is not None:
            properties["Puntos"] = {"number": assignment["points_possible"]}
    # Entregado en Canvas (o marcado en su To-Do) cuenta como hecho.
    done_in_canvas = canvas_complete or delivered

    existing = find_existing_page(data_source_id, canvas_id)
    name = properties["Name"]["title"][0]["text"]["content"]

    if existing:
        notion_done = get_notion_status(existing) == "Done"
        if notion_done and not canvas_complete:
            # Notion -> Canvas: el estudiante lo marcó "Done" en Notion.
            mark_canvas_complete(item)
        elif done_in_canvas and not notion_done:
            # Canvas -> Notion: se entregó o se marcó como hecho en Canvas.
            properties["Status"] = {"status": {"name": "Done"}}
        page = existing
        if update_page(existing, properties):
            print(f"Actualizado: {name}")
    elif is_stale(item.get("plannable_date"), done_in_canvas):
        # Ya se archivó en una corrida anterior (o se archivaría al final de
        # esta): el planner lo sigue devolviendo, pero no se vuelve a crear.
        return
    else:
        if done_in_canvas:
            properties["Status"] = {"status": {"name": "Done"}}
        page = notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": data_source_id},
            properties=properties,
        )
        print(f"Creado: {name}")

    sync_page_details(page["id"], planner_item_details(item, assignments))


def upsert_announcement(data_source_id, announcement, course_names):
    canvas_id = f'announcement-{announcement["id"]}'
    context_code = announcement.get("context_code", "")
    course_id = int(context_code.split("_", 1)[1]) if context_code.startswith("course_") else None

    properties = {
        "Name": {"title": [{"text": {"content": announcement.get("title", "Sin título")}}]},
        "Type": {"select": {"name": "Announcement"}},
        "Course": {"select": {"name": course_names.get(course_id, "General")}},
        "Canvas ID": {"rich_text": [{"text": {"content": canvas_id}}]},
        "Canvas Link": {"url": build_canvas_link(announcement.get("html_url"))},
    }
    if announcement.get("posted_at"):
        properties["Due Date"] = {"date": {"start": announcement["posted_at"]}}

    existing = find_existing_page(data_source_id, canvas_id)
    name = properties["Name"]["title"][0]["text"]["content"]

    if existing:
        page = existing
        if update_page(existing, properties):
            print(f"Actualizado (anuncio): {name}")
    else:
        page = notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": data_source_id},
            properties=properties,
        )
        print(f"Creado (anuncio): {name}")

    author = (announcement.get("author") or {}).get("display_name")
    facts = [f"**Publicado por:** {escape_text(author)}"] if author else []
    sync_page_details(page["id"], details_markdown(announcement.get("message"), facts, heading="Mensaje"))


def upsert_module_resource(data_source_id, course_id, item, course_names):
    canvas_id = f'module_item-{item["id"]}'
    link = item.get("html_url") or item.get("external_url")

    properties = {
        "Name": {"title": [{"text": {"content": item.get("title", "Sin título")}}]},
        "Type": {"select": {"name": MODULE_RESOURCE_TYPE_LABELS.get(item.get("type"), "Resource")}},
        "Course": {"select": {"name": course_names.get(course_id, "General")}},
        "Canvas ID": {"rich_text": [{"text": {"content": canvas_id}}]},
        "Canvas Link": {"url": build_canvas_link(link)},
    }

    existing = find_existing_page(data_source_id, canvas_id)
    name = properties["Name"]["title"][0]["text"]["content"]

    if existing:
        page = existing
        if update_page(existing, properties):
            print(f"Actualizado (recurso): {name}")
    else:
        page = notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": data_source_id},
            properties=properties,
        )
        print(f"Creado (recurso): {name}")

    # Las páginas de Canvas traen su contenido y los PDFs se copian enteros;
    # el resto de recursos (links, otros archivos) se quedan solo con el link.
    if item.get("type") == "Page" and item.get("url"):
        canvas_page = canvas_get_optional(item["url"])
        if canvas_page:
            sync_page_details(page["id"], details_markdown(canvas_page.get("body"), heading="Contenido"))
    elif item.get("type") == "File" and item.get("url"):
        sync_pdf(page["id"], item)


def upsert_course_grade(data_source_id, course_id, course_name, course_grades):
    grade = course_grades.get(course_id) or {}
    score = grade.get("current_score")
    letter = grade.get("current_grade")
    if score is None and letter is None:
        return  # curso sin notas cargadas todavía

    text = f"{score}%" if score is not None else ""
    if letter:
        text = f"{text} ({letter})" if text else letter

    canvas_id = f"course_grade-{course_id}"
    properties = {
        "Name": {"title": [{"text": {"content": f"Nota general: {course_name}"}}]},
        "Type": {"select": {"name": "Course Grade"}},
        "Course": {"select": {"name": course_name}},
        "Canvas ID": {"rich_text": [{"text": {"content": canvas_id}}]},
        "Canvas Link": {"url": build_canvas_link(grade.get("html_url"))},
        "Grade": {"rich_text": [{"text": {"content": text}}]},
    }
    if score is not None:
        properties["Nota %"] = {"number": score}

    existing = find_existing_page(data_source_id, canvas_id)

    if existing:
        if update_page(existing, properties):
            print(f"Actualizado (nota general): {course_name}")
    else:
        notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": data_source_id},
            properties=properties,
        )
        print(f"Creado (nota general): {course_name}")


def is_stale(due_date, done):
    # Mismo criterio que archive_stale_items, para un item que aún no está en Notion.
    if not due_date:
        return False
    due = datetime.fromisoformat(due_date.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    today = datetime.now(timezone.utc).date()
    return (done and due < today) or due < today - timedelta(days=ARCHIVE_OVERDUE_AFTER_DAYS)


def archive_stale_items(data_source_id):
    today = datetime.now(timezone.utc).date().isoformat()
    grace_cutoff = (
        datetime.now(timezone.utc).date() - timedelta(days=ARCHIVE_OVERDUE_AFTER_DAYS)
    ).isoformat()

    # Se archiva lo vencido y ya hecho de inmediato, y lo vencido sin hacer
    # después del periodo de gracia (para no perder de vista lo no entregado
    # demasiado pronto, pero tampoco acumularlo para siempre).
    stale_filter = {
        "or": [
            {
                "and": [
                    {"property": "Due Date", "date": {"before": today}},
                    {"property": "Status", "status": {"equals": "Done"}},
                ]
            },
            {"property": "Due Date", "date": {"before": grace_cutoff}},
        ]
    }

    archived_count = 0
    cursor = None
    while True:
        kwargs = {"data_source_id": data_source_id, "filter": stale_filter}
        if cursor:
            kwargs["start_cursor"] = cursor
        result = notion.data_sources.query(**kwargs)
        for page in result["results"]:
            notion.pages.update(page_id=page["id"], archived=True)
            archived_count += 1
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")

    print(f"{archived_count} items archivados (vencidos)")


def main():
    data_source_id = get_data_source_id()
    ensure_schema(data_source_id)

    courses = fetch_active_courses()
    course_names = {course["id"]: course.get("name", "General") for course in courses}
    course_ids = list(course_names.keys())

    assignments = fetch_assignments(course_ids)
    course_grades = fetch_course_grades()

    items = fetch_planner_items()
    print(f"{len(items)} items encontrados en Canvas Planner")
    for item in items:
        upsert_item(data_source_id, item, assignments)

    announcements = fetch_announcements(course_ids)
    print(f"{len(announcements)} anuncios encontrados en cursos activos")
    for announcement in announcements:
        upsert_announcement(data_source_id, announcement, course_names)

    resources = fetch_module_resources(course_ids)
    print(f"{len(resources)} recursos de módulos encontrados")
    for course_id, item in resources:
        upsert_module_resource(data_source_id, course_id, item, course_names)

    for course_id, course_name in course_names.items():
        upsert_course_grade(data_source_id, course_id, course_name, course_grades)

    archive_stale_items(data_source_id)


if __name__ == "__main__":
    main()
