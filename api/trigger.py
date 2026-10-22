"""Relay para el botón "Sincronizar ahora" de Notion.

Notion manda un webhook (POST) con el header X-Trigger-Secret; si coincide con
TRIGGER_SECRET, esta función dispara el workflow sync.yml vía workflow_dispatch.

Límite de uso: el historial de corridas manuales en GitHub Actions hace de
contador (sin base de datos). Se rechaza con 429 si ya hubo
TRIGGER_DAILY_LIMIT corridas manuales en las últimas 24 h, o si la última fue
hace menos de TRIGGER_COOLDOWN_MINUTES. Las corridas lanzadas a mano desde
GitHub (Run workflow) también cuentan.

Variables de entorno (en Vercel):
  TRIGGER_SECRET            secreto compartido con el header del webhook de Notion
  GITHUB_TOKEN              PAT fine-grained con "Actions: Read and write" sobre este repo
  GITHUB_REPO               opcional, default SantiMerino/canvas-notion-sync
  GITHUB_REF                opcional, default master
  TRIGGER_DAILY_LIMIT       opcional, default 8 (corridas manuales por 24 h)
  TRIGGER_COOLDOWN_MINUTES  opcional, default 10 (minutos entre corridas manuales)
"""

import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

WORKFLOW_FILE = "sync.yml"


def github(path, body=None):
    repo = os.environ.get("GITHUB_REPO", "SantiMerino/canvas-notion-sync")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "canvas-notion-sync-trigger",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500]


def check_quota(now):
    """Devuelve None si se puede disparar, o (status, body) para rechazar."""
    limit = int(os.environ.get("TRIGGER_DAILY_LIMIT", "8"))
    cooldown = timedelta(minutes=int(os.environ.get("TRIGGER_COOLDOWN_MINUTES", "10")))
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode(
        {"event": "workflow_dispatch", "created": f">={since}", "per_page": 1}
    )
    status, data = github(f"runs?{query}")
    if status != 200:
        # Fallar cerrado: si no se puede contar, no se dispara.
        return 502, {"ok": False, "github_status": status, "detail": data}

    if data["total_count"] >= limit:
        return 429, {"ok": False, "error": f"límite de {limit} syncs manuales en 24 h alcanzado"}

    runs = data["workflow_runs"]
    if runs:
        last = datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))
        wait = last + cooldown - now
        if wait > timedelta(0):
            minutes = int(wait.total_seconds() // 60) + 1
            return 429, {"ok": False, "error": f"espera {minutes} min antes del próximo sync manual"}
    return None


class handler(BaseHTTPRequestHandler):
    def _reply(self, status, body):
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        expected = os.environ.get("TRIGGER_SECRET", "").encode()
        # Comparar bytes: compare_digest lanza TypeError con str no-ASCII (500 en vez de 401).
        given = self.headers.get("X-Trigger-Secret", "").encode("utf-8", "surrogateescape")
        if not expected or not hmac.compare_digest(given, expected):
            return self._reply(401, {"ok": False, "error": "unauthorized"})

        rejected = check_quota(datetime.now(timezone.utc))
        if rejected:
            return self._reply(*rejected)

        status, detail = github("dispatches", {"ref": os.environ.get("GITHUB_REF", "master")})
        if status == 204:
            return self._reply(200, {"ok": True, "message": "Sync en cola en GitHub Actions"})
        return self._reply(502, {"ok": False, "github_status": status, "detail": detail})

    def do_GET(self):
        self._reply(405, {"ok": False, "error": "use POST"})
