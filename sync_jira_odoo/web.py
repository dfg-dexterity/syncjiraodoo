"""Interface web local de monitoramento e configuração do de-para.

    python3 -m sync_jira_odoo.web --port 8765

Permite editar a tabela de-para (mapping.json), validar credenciais,
disparar sync/dry-run e acompanhar histórico, avisos e log — tudo com a
stdlib (http.server). Pensada para uso local/rede interna: não há
autenticação, não exponha a porta publicamente.
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import storage
from .config import Config, ConfigError
from .jira_client import JiraClient
from .mapping import Mapping
from .odoo_client import OdooClient
from .sync import SyncEngine, connection_report

log = logging.getLogger("sync_jira_odoo")


class _BufferHandler(logging.Handler):
    def __init__(self, buffer: collections.deque):
        super().__init__()
        self.buffer = buffer
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.buffer.append(self.format(record))


class SyncRunner:
    """Executa o sync em uma thread de fundo, capturando o log para a UI."""

    def __init__(
        self,
        mapping_path: Path,
        state_path: Path,
        history_path: Path,
        import_log_path: Path | None = None,
    ):
        self.mapping_path = mapping_path
        self.state_path = state_path
        self.history_path = history_path
        self.import_log_path = import_log_path or Path(storage.DEFAULT_IMPORT_LOG_FILE)
        self.lock = threading.Lock()
        self.running = False
        self.log_buffer: collections.deque[str] = collections.deque(maxlen=400)
        log.addHandler(_BufferHandler(self.log_buffer))

    def start(self, dry_run: bool, since: str | None, delete: bool) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
        self.log_buffer.clear()
        threading.Thread(
            target=self._execute, args=(dry_run, since, delete), daemon=True
        ).start()
        return True

    def _execute(self, dry_run: bool, since_str: str | None, delete: bool) -> None:
        record: dict = {
            "finished_utc": None,
            "dry_run": dry_run,
            "delete": delete,
            "ok": False,
        }
        try:
            cfg = Config.from_env()
            mapping = Mapping.load(self.mapping_path)
            errors = mapping.validate()
            if errors:
                raise ConfigError("mapping inválido: " + "; ".join(errors))
            cfg.apply_mapping(mapping)

            since = storage.load_since(since_str or None, self.state_path)
            run_started = datetime.now(timezone.utc)
            odoo = OdooClient(cfg.odoo_url, cfg.odoo_db, cfg.odoo_user, cfg.odoo_api_key)
            jira = JiraClient(cfg.jira_url, cfg.jira_user, cfg.jira_api_token)
            result = SyncEngine(jira, odoo, cfg).run(since, dry_run=dry_run, delete=delete)

            log.info(
                "fim: %d criados, %d atualizados, %d pulados, %d removidos, %d avisos%s",
                result.created,
                result.updated,
                result.skipped,
                result.deleted,
                len(result.warnings),
                " (dry-run, nada gravado)" if dry_run else "",
            )
            record.update(
                since=since.isoformat(),
                created=result.created,
                updated=result.updated,
                skipped=result.skipped,
                deleted=result.deleted,
                warnings=result.warnings,
                ok=True,
            )
            if not dry_run:
                storage.append_import_items(self.import_log_path, result.items)
                storage.save_state(self.state_path, run_started)
        except Exception as exc:  # mostra qualquer falha no monitor
            log.error("%s", exc)
            record["error"] = str(exc)
        finally:
            record["finished_utc"] = datetime.now(timezone.utc).isoformat()
            storage.append_history(self.history_path, record)
            with self.lock:
                self.running = False


class App(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], runner: SyncRunner):
        super().__init__(address, Handler)
        self.runner = runner


class Handler(BaseHTTPRequestHandler):
    server: App

    def log_message(self, format: str, *args) -> None:  # silencia o access log
        pass

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        runner = self.server.runner
        if self.path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/import-log"):
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = min(int(query.get("limit", ["500"])[0]), 2000)
            except ValueError:
                limit = 500
            entries = storage.read_import_log(runner.import_log_path, limit)
            self._send_json({"items": list(reversed(entries))})
        elif self.path == "/api/state":
            mapping = Mapping.load(runner.mapping_path)
            history = storage.read_history(runner.history_path)
            self._send_json(
                {
                    "running": runner.running,
                    "mapping": {
                        "restrict_to_mapped_projects": mapping.restrict_to_mapped_projects,
                        "projects": mapping.projects,
                        "users": mapping.users,
                    },
                    "history": list(reversed(history[-50:])),
                    "log": list(runner.log_buffer),
                }
            )
        else:
            self._send_json({"error": "não encontrado"}, status=404)

    def do_POST(self) -> None:
        runner = self.server.runner
        if self.path == "/api/mapping":
            data = self._read_json()
            mapping = Mapping(
                restrict_to_mapped_projects=bool(data.get("restrict_to_mapped_projects")),
                projects=list(data.get("projects", [])),
                users=list(data.get("users", [])),
            )
            errors = mapping.validate()
            if errors:
                self._send_json({"ok": False, "errors": errors}, status=400)
                return
            mapping.save(runner.mapping_path)
            self._send_json({"ok": True})
        elif self.path == "/api/sync":
            data = self._read_json()
            started = runner.start(
                dry_run=bool(data.get("dry_run")),
                since=data.get("since") or None,
                delete=bool(data.get("delete")),
            )
            if started:
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": "sincronização já em execução"}, 409)
        elif self.path == "/api/test":
            try:
                detail = connection_report(Config.from_env())
                self._send_json({"ok": True, "detail": detail})
            except Exception as exc:
                self._send_json({"ok": False, "detail": str(exc)})
        else:
            self._send_json({"error": "não encontrado"}, status=404)


INDEX_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sync Jira/Clockwork → Odoo</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 1100px;
         padding: 0 1rem; color: #1f2937; }
  h1 { font-size: 1.5rem; } h2 { font-size: 1.15rem; margin-top: 2rem; }
  table { border-collapse: collapse; width: 100%; margin: .5rem 0; }
  th, td { border: 1px solid #d1d5db; padding: .35rem .55rem; text-align: left;
           font-size: .92rem; }
  th { background: #f3f4f6; }
  td input[type=text] { width: 100%; box-sizing: border-box; border: none;
                        background: transparent; font: inherit; }
  button { padding: .45rem .9rem; margin: .15rem .35rem .15rem 0; cursor: pointer;
           border: 1px solid #9ca3af; border-radius: 6px; background: #f9fafb; }
  button:hover { background: #eef2ff; }
  .muted { color: #6b7280; font-weight: normal; font-size: .85rem; }
  .ok { color: #15803d; } .warn { color: #b45309; } .err { color: #b91c1c; }
  pre { background: #111827; color: #e5e7eb; padding: 1rem; border-radius: 6px;
        max-height: 320px; overflow: auto; font-size: .8rem; }
  #msg { margin: .5rem 0; white-space: pre-wrap; }
  .del { color: #b91c1c; border: none; background: none; font-size: 1rem; }
</style>
</head>
<body>
<h1>Sincronização Jira/Clockwork → Odoo</h1>
<div>Status: <span id="status">…</span></div>

<h2>De-para de projetos <span class="muted">vários projetos Jira podem apontar
para o mesmo projeto Odoo</span></h2>
<table id="projTable">
  <thead><tr><th style="width:45%">Projeto no Odoo (nome exato)</th>
  <th>Projetos no Jira (keys separadas por vírgula, ex.: CDV, CCDV)</th>
  <th style="width:2rem"></th></tr></thead>
  <tbody></tbody>
</table>
<button onclick="addProjectRow()">+ adicionar projeto</button>
<label><input type="checkbox" id="restrict"> sincronizar somente projetos mapeados</label>

<h2>De-para de usuários</h2>
<table id="userTable">
  <thead><tr><th>Usuário Jira (e-mail ou accountId)</th>
  <th>Funcionário Odoo (e-mail)</th><th>Nome (opcional)</th>
  <th style="width:2rem"></th></tr></thead>
  <tbody></tbody>
</table>
<button onclick="addUserRow()">+ adicionar usuário</button>

<h2>Ações</h2>
<p>
  <button onclick="saveMapping()">💾 Salvar de-para</button>
  <button onclick="testConnection()">🔌 Testar conexões</button>
  <button onclick="runSync(true)">🧪 Dry-run</button>
  <button onclick="runSync(false)">▶ Sincronizar agora</button>
  <label>desde: <input type="date" id="since"></label>
  <label><input type="checkbox" id="propDelete"> propagar exclusões do Jira</label>
</p>
<div id="msg"></div>

<h2>Monitor</h2>
<div id="lastRun" class="muted">nenhuma execução registrada ainda</div>
<table id="histTable">
  <thead><tr><th>Quando (local)</th><th>Tipo</th><th>Desde</th><th>Criados</th>
  <th>Atualizados</th><th>Pulados</th><th>Removidos</th><th>Avisos</th>
  <th>Resultado</th></tr></thead>
  <tbody></tbody>
</table>
<h2>Log da última execução</h2>
<pre id="log"></pre>

<h2>Importados no Odoo <span class="muted">últimos registros gravados
(dry-run não entra aqui)</span></h2>
<p>
  <input type="text" id="implogFilter" placeholder="filtrar por issue, autor, texto…"
         style="padding:.35rem .55rem; min-width: 260px;">
  <button onclick="loadImportLog()">↻ atualizar</button>
</p>
<table id="implogTable">
  <thead><tr><th>Gravado em (local)</th><th>Ação</th><th>Issue</th>
  <th>Data</th><th>Horas</th><th>Autor</th><th>Descrição</th></tr></thead>
  <tbody></tbody>
</table>

<script>
let mappingLoaded = false;

function el(tag, attrs = {}, text = "") {
  const node = document.createElement(tag);
  Object.assign(node, attrs);
  if (text) node.textContent = text;
  return node;
}

function projectRow(odoo = "", jiraKeys = "") {
  const tr = el("tr");
  for (const value of [odoo, jiraKeys]) {
    const td = el("td");
    td.appendChild(el("input", { type: "text", value }));
    tr.appendChild(td);
  }
  const td = el("td");
  const del = el("button", { className: "del", title: "remover" }, "✕");
  del.onclick = () => tr.remove();
  td.appendChild(del);
  tr.appendChild(td);
  return tr;
}

function userRow(jira = "", odoo = "", nome = "") {
  const tr = el("tr");
  for (const value of [jira, odoo, nome]) {
    const td = el("td");
    td.appendChild(el("input", { type: "text", value }));
    tr.appendChild(td);
  }
  const td = el("td");
  const del = el("button", { className: "del", title: "remover" }, "✕");
  del.onclick = () => tr.remove();
  td.appendChild(del);
  tr.appendChild(td);
  return tr;
}

function addProjectRow() {
  document.querySelector("#projTable tbody").appendChild(projectRow());
}
function addUserRow() {
  document.querySelector("#userTable tbody").appendChild(userRow());
}

function renderMapping(mapping) {
  const projBody = document.querySelector("#projTable tbody");
  projBody.innerHTML = "";
  for (const row of mapping.projects)
    projBody.appendChild(projectRow(row.odoo || "", (row.jira || []).join(", ")));
  const userBody = document.querySelector("#userTable tbody");
  userBody.innerHTML = "";
  for (const row of mapping.users)
    userBody.appendChild(userRow(row.jira || "", row.odoo || "", row.nome || ""));
  document.getElementById("restrict").checked = !!mapping.restrict_to_mapped_projects;
}

function collectMapping() {
  const projects = [...document.querySelectorAll("#projTable tbody tr")].map(tr => {
    const [odoo, jira] = [...tr.querySelectorAll("input")].map(i => i.value.trim());
    return { odoo, jira: jira.split(",").map(s => s.trim()).filter(Boolean) };
  }).filter(r => r.odoo || r.jira.length);
  const users = [...document.querySelectorAll("#userTable tbody tr")].map(tr => {
    const [jira, odoo, nome] = [...tr.querySelectorAll("input")].map(i => i.value.trim());
    return { jira, odoo, nome };
  }).filter(r => r.jira || r.odoo);
  return {
    restrict_to_mapped_projects: document.getElementById("restrict").checked,
    projects, users,
  };
}

function showMsg(text, cls) {
  const msg = document.getElementById("msg");
  msg.textContent = text;
  msg.className = cls || "";
}

async function saveMapping() {
  const res = await fetch("/api/mapping", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collectMapping()),
  });
  const data = await res.json();
  if (data.ok) showMsg("De-para salvo em mapping.json ✔", "ok");
  else showMsg("Erros no de-para:\\n" + (data.errors || [data.error]).join("\\n"), "err");
}

async function testConnection() {
  showMsg("Testando conexões…");
  const res = await fetch("/api/test", { method: "POST" });
  const data = await res.json();
  showMsg(data.detail, data.ok ? "ok" : "err");
}

async function runSync(dryRun) {
  const res = await fetch("/api/sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      dry_run: dryRun,
      since: document.getElementById("since").value || null,
      delete: document.getElementById("propDelete").checked,
    }),
  });
  const data = await res.json();
  showMsg(data.ok ? (dryRun ? "Dry-run iniciado…" : "Sincronização iniciada…")
                  : data.error, data.ok ? "" : "warn");
}

function renderHistory(history) {
  const body = document.querySelector("#histTable tbody");
  body.innerHTML = "";
  for (const run of history) {
    const tr = el("tr");
    tr.appendChild(el("td", {}, run.finished_utc
      ? new Date(run.finished_utc).toLocaleString() : "—"));
    tr.appendChild(el("td", {}, run.dry_run ? "dry-run" : "sync"));
    tr.appendChild(el("td", {}, run.since
      ? new Date(run.since).toLocaleDateString() : "—"));
    for (const field of ["created", "updated", "skipped", "deleted"])
      tr.appendChild(el("td", {}, String(run[field] ?? "—")));
    tr.appendChild(el("td", {}, String((run.warnings || []).length)));
    const result = el("td", {}, run.ok ? "✔ ok" : "✖ " + (run.error || "erro"));
    result.className = run.ok ? "ok" : "err";
    tr.appendChild(result);
    body.appendChild(tr);
  }
  const last = history[0];
  const lastRun = document.getElementById("lastRun");
  if (!last) return;
  if ((last.warnings || []).length) {
    lastRun.className = "warn";
    lastRun.textContent = "Avisos da última execução:\\n• " + last.warnings.join("\\n• ");
    lastRun.style.whiteSpace = "pre-wrap";
  } else {
    lastRun.className = "muted";
    lastRun.textContent = "Última execução sem avisos.";
  }
}

let importLog = [];

async function loadImportLog() {
  try {
    const res = await fetch("/api/import-log?limit=500");
    importLog = (await res.json()).items || [];
    renderImportLog();
  } catch (e) { /* servidor ocupado; o botão ↻ tenta de novo */ }
}

function renderImportLog() {
  const filter = (document.getElementById("implogFilter").value || "").toLowerCase();
  const body = document.querySelector("#implogTable tbody");
  body.innerHTML = "";
  for (const it of importLog) {
    const hay = [it.issue, it.autor, it.descricao, it.acao, it.data]
      .filter(Boolean).join(" ").toLowerCase();
    if (filter && !hay.includes(filter)) continue;
    const tr = el("tr");
    tr.appendChild(el("td", {}, it.logged_utc
      ? new Date(it.logged_utc).toLocaleString() : "—"));
    tr.appendChild(el("td", {}, it.acao || "—"));
    tr.appendChild(el("td", {}, it.issue || (it.worklog ? "worklog " + it.worklog : "—")));
    tr.appendChild(el("td", {}, it.data || "—"));
    tr.appendChild(el("td", {}, it.horas != null ? String(it.horas) : "—"));
    tr.appendChild(el("td", {}, it.autor || "—"));
    tr.appendChild(el("td", {}, it.descricao || "—"));
    body.appendChild(tr);
  }
}

document.getElementById("implogFilter").addEventListener("input", renderImportLog);

let wasRunning = false;

async function refresh() {
  try {
    const state = await (await fetch("/api/state")).json();
    document.getElementById("status").innerHTML = state.running
      ? '<b class="warn">⏳ sincronização em execução…</b>'
      : '<span class="ok">ocioso</span>';
    if (!mappingLoaded) { renderMapping(state.mapping); mappingLoaded = true; }
    renderHistory(state.history);
    document.getElementById("log").textContent = state.log.join("\\n");
    if (wasRunning && !state.running) loadImportLog();
    wasRunning = state.running;
  } catch (e) { /* servidor reiniciando; tenta de novo no próximo ciclo */ }
}

refresh();
loadImportLog();
setInterval(refresh, 2500);
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sync_jira_odoo.web",
        description="Interface web local do sync Jira/Clockwork → Odoo.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mapping-file", default="mapping.json")
    parser.add_argument("--state-file", default=storage.DEFAULT_STATE_FILE)
    parser.add_argument("--history-file", default=storage.DEFAULT_HISTORY_FILE)
    parser.add_argument("--import-log-file", default=storage.DEFAULT_IMPORT_LOG_FILE)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    runner = SyncRunner(
        Path(args.mapping_file),
        Path(args.state_file),
        Path(args.history_file),
        Path(args.import_log_file),
    )
    server = App((args.host, args.port), runner)
    print(f"Monitor disponível em http://{args.host}:{server.server_address[1]}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
