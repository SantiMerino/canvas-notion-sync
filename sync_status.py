"""Reporta el estado de cada corrida en la base "Estado de sincronización" de Notion.

Es best-effort: si falta la configuración o Notion falla, imprime un aviso y
sigue; nunca tumba el sync. Usa solo la stdlib para poder correr también desde
el workflow cuando falla `pip install`.

Desde el workflow (paso final si el job falla o se cancela):
    python sync_status.py fail <job.status>
Solo escribe si sync.py no alcanzó a reportar su propio final.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

PROCESS_NAME = "Canvas → Notion"
NOTION_VERSION = "2025-09-03"  # primera versión con data sources
# Marcas locales del runner: sync.py las crea al reportar su inicio y su final,
# para que el paso de falla del workflow no pise un resultado más detallado.
STARTED_MARKER = ".sync_status_started"
REPORTED_MARKER = ".sync_status_reported"

_row_id = None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_url():
    server, repo, run_id = (
        os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    return f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None


def _notion(path, body=None, method=None):
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method or ("POST" if body is not None else "GET"),
        headers={
            "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _find_row():
    global _row_id
    if _row_id:
        return _row_id
    database = _notion(f"databases/{os.environ['SYNC_STATUS_DATABASE_ID']}")
    data_source_id = database["data_sources"][0]["id"]
    title_filter = {"property": "Proceso", "title": {"equals": PROCESS_NAME}}
    results = _notion(f"data_sources/{data_source_id}/query", {"filter": title_filter})["results"]
    if results:
        _row_id = results[0]["id"]
    else:
        page = _notion(
            "pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": data_source_id},
                "properties": {"Proceso": {"title": [{"text": {"content": PROCESS_NAME}}]}},
            },
        )
        _row_id = page["id"]
    return _row_id


def _date(value):
    return {"date": {"start": value} if value else None}


def _number(value):
    return {"number": value}


def report(state, *, started=None, finished=None, created=None, updated=None, result=""):
    if not os.environ.get("SYNC_STATUS_DATABASE_ID") or not os.environ.get("NOTION_TOKEN"):
        return
    properties = {
        "Estado": {"select": {"name": state}},
        "Última finalización": _date(finished),
        "Nuevos": _number(created),
        "Actualizados": _number(updated),
        "Resultado": {"rich_text": [{"text": {"content": result[:1900]}}] if result else []},
        "Corrida GitHub": {"url": run_url()},
    }
    if started:
        properties["Último intento"] = _date(started)
    try:
        _notion(f"pages/{_find_row()}", {"properties": properties}, method="PATCH")
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as error:
        detail = f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__
        print(f"⚠️ No se pudo actualizar el estado en Notion ({detail})")


def report_start():
    report("En ejecución", started=_now())
    open(STARTED_MARKER, "w").close()


def report_finish(state, *, created, updated, result):
    report(state, finished=_now(), created=created, updated=updated, result=result)
    open(REPORTED_MARKER, "w").close()


def report_job_failure(job_status):
    if os.path.exists(REPORTED_MARKER):
        return  # sync.py ya reportó su final con más detalle
    reason = "cancelada" if job_status == "cancelled" else "falló antes de terminar el sync"
    now = _now()
    # Si sync.py no llegó a arrancar (ej. falló pip), el intento es esta corrida.
    started = None if os.path.exists(STARTED_MARKER) else now
    report("Error", started=started, finished=now, result=f"La corrida {reason}. Ver la corrida de GitHub.")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "fail":
        report_job_failure(sys.argv[2] if len(sys.argv) > 2 else "failure")
    else:
        sys.exit("uso: python sync_status.py fail <job.status>")
