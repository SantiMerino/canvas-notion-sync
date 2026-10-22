"""Relay para el botón "Sincronizar ahora" de Notion.

Notion manda un webhook (POST) con el header X-Trigger-Secret; si coincide con
TRIGGER_SECRET, esta función dispara el workflow sync.yml vía workflow_dispatch.

Variables de entorno (en Vercel):
  TRIGGER_SECRET  secreto compartido con el header del webhook de Notion
  GITHUB_TOKEN    PAT fine-grained con "Actions: Read and write" sobre este repo
  GITHUB_REPO     opcional, default SantiMerino/canvas-notion-sync
  GITHUB_REF      opcional, default master
"""

import hmac
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

WORKFLOW_FILE = "sync.yml"


def dispatch_workflow():
    repo = os.environ.get("GITHUB_REPO", "SantiMerino/canvas-notion-sync")
    ref = os.environ.get("GITHUB_REF", "master")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        data=json.dumps({"ref": ref}).encode(),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "canvas-notion-sync-trigger",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500]


class handler(BaseHTTPRequestHandler):
    def _reply(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        expected = os.environ.get("TRIGGER_SECRET", "")
        given = self.headers.get("X-Trigger-Secret", "")
        if not expected or not hmac.compare_digest(given, expected):
            return self._reply(401, {"ok": False, "error": "unauthorized"})

        status, detail = dispatch_workflow()
        if status == 204:
            return self._reply(200, {"ok": True, "message": "Sync en cola en GitHub Actions"})
        return self._reply(502, {"ok": False, "github_status": status, "detail": detail})

    def do_GET(self):
        self._reply(405, {"ok": False, "error": "use POST"})
