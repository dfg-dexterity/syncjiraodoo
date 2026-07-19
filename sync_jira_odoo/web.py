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
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import webbrowser

from . import storage
from .config import Config, ConfigError
from .envfile import DEFAULT_ENV_FILE, load_env_file, save_env_file
from .jira_client import JiraClient
from .logsetup import DEFAULT_RUN_LOG_FILE, configure_logging
from .mapping import Mapping
from .odoo_client import OdooClient
from .sync import SyncEngine, connection_report

# variáveis gerenciadas pela tela de configuração; as marcadas são segredos
# e nunca voltam para o navegador (só um "está configurada")
CONFIG_KEYS = (
    "ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_API_KEY",
    "JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN", "DEFAULT_EMPLOYEE_EMAIL",
)
SECRET_KEYS = {"ODOO_API_KEY", "JIRA_API_TOKEN"}
REQUIRED_KEYS = tuple(k for k in CONFIG_KEYS if k != "DEFAULT_EMPLOYEE_EMAIL")

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
        env_path: Path | None = None,
    ):
        self.mapping_path = mapping_path
        self.state_path = state_path
        self.history_path = history_path
        self.import_log_path = import_log_path or Path(storage.DEFAULT_IMPORT_LOG_FILE)
        self.env_path = env_path or Path(DEFAULT_ENV_FILE)
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


def _is_configured() -> bool:
    return all(os.environ.get(key, "").strip() for key in REQUIRED_KEYS)


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
        elif self.path == "/api/config":
            fields = {}
            for key in CONFIG_KEYS:
                value = os.environ.get(key, "")
                fields[key] = {
                    "set": bool(value.strip()),
                    "value": "" if key in SECRET_KEYS else value,
                }
            self._send_json({"fields": fields, "configured": _is_configured()})
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
                    "configured": _is_configured(),
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
        if self.path == "/api/config":
            data = self._read_json()
            values = {
                key: str(data[key]).strip()
                for key in CONFIG_KEYS
                if str(data.get(key, "")).strip()
            }
            if not values:
                self._send_json({"ok": False, "error": "nada para salvar"}, status=400)
                return
            os.environ.update(values)
            save_env_file(runner.env_path, values)
            self._send_json({"ok": True, "configured": _is_configured()})
        elif self.path == "/api/mapping":
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


INDEX_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sincronizador de Horas — Jira → Odoo</title>
<style>
  :root {
    --bg: #f6f7f9; --surface: #fff; --surface-2: #eef0f4;
    --ink: #1b2029; --muted: #5d6674; --line: #dfe3ea; --line-strong: #c3c9d4;
    --jira: #1257b8; --jira-bg: #e8f0fb; --odoo: #714b67; --odoo-bg: #f3ecf1;
    --ok: #17703f; --ok-bg: #e4f3ea; --warn: #8a5a0b; --warn-bg: #f7eeda;
    --err: #ab3226; --err-bg: #f9e9e7; --focus: #1257b8;
    --shadow: 0 1px 2px rgba(27,32,41,.06), 0 4px 14px rgba(27,32,41,.05);
    --mono: ui-monospace, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0 0 5rem; background: var(--bg); color: var(--ink);
    font: 15px/1.5 ui-sans-serif, system-ui, "Segoe UI", Roboto, Arial, sans-serif;
  }
  main { max-width: 1080px; margin: 0 auto; padding: 0 1.25rem; }
  .topbar { background: var(--surface); border-bottom: 1px solid var(--line); box-shadow: var(--shadow); }
  .topbar-inner {
    max-width: 1080px; margin: 0 auto; padding: 1rem 1.25rem;
    display: flex; align-items: center; gap: .9rem; flex-wrap: wrap;
  }
  .logo { font-size: 1.5rem; }
  .titles h1 { font-size: 1.15rem; margin: 0; }
  .titles p { margin: 0; font-size: .8rem; color: var(--muted); }
  .titles p b.j { color: var(--jira); } .titles p b.o { color: var(--odoo); }
  .spacer { flex: 1; }
  .pill { font-size: .8rem; font-weight: 600; padding: .28rem .75rem; border-radius: 999px; }
  .pill.ok { color: var(--ok); background: var(--ok-bg); }
  .pill.warn { color: var(--warn); background: var(--warn-bg); }
  .pill.err { color: var(--err); background: var(--err-bg); }

  .card {
    background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
    padding: 1.1rem 1.25rem; margin-top: 1.25rem; box-shadow: var(--shadow);
  }
  .card h2 { font-size: 1.02rem; margin: 0 0 .25rem; }
  .card .sub { font-size: .82rem; color: var(--muted); margin: 0 0 .9rem; }

  button {
    font: inherit; font-weight: 600; cursor: pointer; border-radius: 9px;
    border: 1px solid var(--line-strong); background: var(--surface); color: var(--ink);
    padding: .5rem 1rem;
  }
  button:hover { background: var(--surface-2); }
  button:focus-visible, input:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
  button.big {
    font-size: 1.02rem; padding: .75rem 1.5rem; border-radius: 11px;
    background: var(--ink); color: var(--bg); border-color: var(--ink);
  }
  button.big:hover { opacity: .88; }
  button.big.ghost { background: var(--surface); color: var(--ink); border-color: var(--line-strong); }
  button.big.ghost:hover { background: var(--surface-2); opacity: 1; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.mini { font-size: .78rem; padding: .25rem .6rem; }

  .summary { display: flex; gap: .75rem; flex-wrap: wrap; margin-bottom: 1rem; }
  .stat {
    background: var(--surface-2); border-radius: 10px; padding: .55rem .9rem;
    min-width: 7.5rem;
  }
  .stat b { display: block; font-size: 1.25rem; font-variant-numeric: tabular-nums; }
  .stat span { font-size: .74rem; color: var(--muted); }
  #lastRunLine { font-size: .86rem; color: var(--muted); margin: 0 0 .9rem; }
  #lastRunLine.err { color: var(--err); }
  .actions { display: flex; gap: .7rem; flex-wrap: wrap; align-items: center; }
  #warnBox {
    display: none; margin-top: .9rem; padding: .7rem .95rem; border-radius: 10px;
    background: var(--warn-bg); color: var(--warn); font-size: .84rem; white-space: pre-wrap;
  }

  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  @media (max-width: 760px) { .grid2 { grid-template-columns: 1fr; } }
  fieldset {
    border: 1px solid var(--line); border-radius: 11px; padding: .8rem 1rem 1rem; margin: 0;
  }
  fieldset legend { font-size: .8rem; font-weight: 700; padding: 0 .35rem; }
  fieldset.f-jira legend { color: var(--jira); }
  fieldset.f-odoo legend { color: var(--odoo); }
  .field { margin-top: .6rem; }
  .field label { display: block; font-size: .76rem; font-weight: 600; color: var(--muted); margin-bottom: .2rem; }
  .field input {
    width: 100%; font: inherit; font-size: .88rem; padding: .45rem .6rem;
    border: 1px solid var(--line-strong); border-radius: 8px;
    background: var(--surface); color: var(--ink);
  }
  #cfgMsg, #msg { margin-top: .7rem; font-size: .85rem; white-space: pre-wrap; }
  .ok-text { color: var(--ok); } .err-text { color: var(--err); } .warn-text { color: var(--warn); }

  table { border-collapse: collapse; width: 100%; margin: .4rem 0; font-size: .88rem; }
  th, td { border: 1px solid var(--line); padding: .38rem .55rem; text-align: left; }
  th { background: var(--surface-2); font-size: .78rem; }
  td input[type=text] { width: 100%; border: none; background: transparent; font: inherit; }
  .del { color: var(--err); border: none; background: none; font-size: 1rem; cursor: pointer; }
  .muted { color: var(--muted); font-weight: normal; font-size: .8rem; }
  .table-wrap { overflow-x: auto; }
  details.adv { margin-top: .8rem; }
  details.adv summary { cursor: pointer; font-size: .82rem; color: var(--muted); }
  details.adv .inner { margin-top: .6rem; display: flex; gap: 1.1rem; flex-wrap: wrap; font-size: .86rem; }
  pre {
    background: #111827; color: #e5e7eb; padding: .9rem; border-radius: 9px;
    max-height: 300px; overflow: auto; font-size: .76rem;
  }
  #implogFilter { padding: .4rem .6rem; min-width: 250px; border: 1px solid var(--line-strong); border-radius: 8px; font: inherit; font-size: .85rem; }
</style>
</head>
<body>

<div class="topbar"><div class="topbar-inner">
  <div class="logo">⏱</div>
  <div class="titles">
    <h1>Sincronizador de Horas</h1>
    <p>leva os apontamentos do <b class="j">Jira/Clockwork</b> para as planilhas de horas do <b class="o">Odoo</b></p>
  </div>
  <span class="spacer"></span>
  <span id="status" class="pill ok">…</span>
</div></div>

<main>

<!-- ============ SINCRONIZAR ============ -->
<section class="card">
  <h2>Sincronizar</h2>
  <p class="sub">Traz para o Odoo tudo que mudou no Jira desde a última execução. Rodar duas vezes não duplica nada.</p>
  <div class="summary" id="statCards" style="display:none">
    <div class="stat"><b id="stCreated">–</b><span>importados</span></div>
    <div class="stat"><b id="stUpdated">–</b><span>atualizados</span></div>
    <div class="stat"><b id="stWarnings">–</b><span>avisos</span></div>
  </div>
  <p id="lastRunLine">nenhuma execução registrada ainda</p>
  <div class="actions">
    <button class="big" id="btnSync">▶ Sincronizar agora</button>
    <button class="big ghost" id="btnDry">Simular (não grava nada)</button>
    <span id="msg"></span>
  </div>
  <div id="warnBox"></div>
  <details class="adv">
    <summary>Opções avançadas</summary>
    <div class="inner">
      <label>buscar desde: <input type="date" id="since"></label>
      <label><input type="checkbox" id="propDelete"> apagar no Odoo os apontamentos excluídos no Jira</label>
    </div>
  </details>
</section>

<!-- ============ CONEXÕES ============ -->
<section class="card">
  <h2>Conexões <span id="cfgState" class="pill warn" style="display:none">configure para começar</span></h2>
  <p class="sub">Preencha uma vez; fica salvo com segurança neste computador (arquivo .env, fora do controle de versão).</p>
  <details id="cfgDetails">
    <summary style="cursor:pointer; font-size:.86rem; color:var(--muted)">mostrar/editar credenciais</summary>
    <div class="grid2" style="margin-top:.9rem">
      <fieldset class="f-jira">
        <legend>Jira — origem dos apontamentos</legend>
        <div class="field"><label>Endereço</label>
          <input id="cfg_JIRA_URL" placeholder="https://suaempresa.atlassian.net"></div>
        <div class="field"><label>E-mail da conta</label>
          <input id="cfg_JIRA_USER" placeholder="voce@suaempresa.com.br"></div>
        <div class="field"><label>Token de API <span class="muted">(id.atlassian.com → Security → API tokens)</span></label>
          <input id="cfg_JIRA_API_TOKEN" type="password" autocomplete="off"></div>
      </fieldset>
      <fieldset class="f-odoo">
        <legend>Odoo — destino das horas</legend>
        <div class="field"><label>Endereço</label>
          <input id="cfg_ODOO_URL" placeholder="https://suaempresa.odoo.com"></div>
        <div class="field"><label>Banco de dados</label>
          <input id="cfg_ODOO_DB" placeholder="suaempresa"></div>
        <div class="field"><label>Usuário (e-mail)</label>
          <input id="cfg_ODOO_USER" placeholder="voce@suaempresa.com.br"></div>
        <div class="field"><label>Chave de API <span class="muted">(Preferências → Segurança da Conta)</span></label>
          <input id="cfg_ODOO_API_KEY" type="password" autocomplete="off"></div>
      </fieldset>
    </div>
    <div class="actions" style="margin-top:.9rem">
      <button id="btnSaveCfg">Salvar conexões</button>
      <button id="btnTest">Testar conexões</button>
    </div>
    <div id="cfgMsg"></div>
  </details>
</section>

<!-- ============ DE-PARA ============ -->
<section class="card">
  <h2>De-para de projetos <span class="muted">— qual projeto do Jira vira qual projeto do Odoo</span></h2>
  <p class="sub">Vários projetos do Jira podem apontar para o mesmo projeto do Odoo. O nome do Odoo precisa ser exatamente igual ao cadastrado lá.</p>
  <div class="table-wrap">
  <table id="projTable">
    <thead><tr><th style="width:45%">Projeto no Odoo (nome exato)</th>
    <th>Projetos no Jira (keys separadas por vírgula, ex.: CDV, CCDV)</th>
    <th style="width:2rem"></th></tr></thead>
    <tbody></tbody>
  </table>
  </div>
  <button class="mini" onclick="addProjectRow()">+ adicionar projeto</button>
  <label style="font-size:.85rem; margin-left:.8rem"><input type="checkbox" id="restrict"> sincronizar somente projetos mapeados</label>

  <h2 style="margin-top:1.4rem">De-para de pessoas</h2>
  <p class="sub">Quem aponta no Jira → qual funcionário recebe as horas no Odoo.</p>
  <div class="table-wrap">
  <table id="userTable">
    <thead><tr><th>Pessoa no Jira (e-mail ou accountId)</th>
    <th>Funcionário no Odoo (e-mail)</th><th>Nome (opcional)</th>
    <th style="width:2rem"></th></tr></thead>
    <tbody></tbody>
  </table>
  </div>
  <button class="mini" onclick="addUserRow()">+ adicionar pessoa</button>
  <div class="actions" style="margin-top:.8rem">
    <button id="btnSaveMapping">💾 Salvar de-para</button>
    <span id="mapMsg" style="font-size:.85rem"></span>
  </div>
</section>

<!-- ============ ATIVIDADE ============ -->
<section class="card">
  <h2>Atividade</h2>
  <p class="sub">Tudo fica registrado: o resumo de cada execução e cada apontamento que entrou no Odoo.</p>

  <h3 style="font-size:.92rem; margin:.4rem 0">Últimas execuções</h3>
  <div class="table-wrap">
  <table id="histTable">
    <thead><tr><th>Quando</th><th>Tipo</th><th>Desde</th><th>Importados</th>
    <th>Atualizados</th><th>Pulados</th><th>Removidos</th><th>Avisos</th><th>Resultado</th></tr></thead>
    <tbody></tbody>
  </table>
  </div>

  <h3 style="font-size:.92rem; margin:1.2rem 0 .4rem">Apontamentos importados no Odoo
    <span class="muted">(simulações não entram aqui)</span></h3>
  <p style="margin:.2rem 0 .5rem">
    <input type="text" id="implogFilter" placeholder="filtrar por issue, pessoa, texto…">
    <button class="mini" onclick="loadImportLog()">↻ atualizar</button>
  </p>
  <div class="table-wrap">
  <table id="implogTable">
    <thead><tr><th>Gravado em</th><th>Ação</th><th>Issue</th><th>Data</th>
    <th>Horas</th><th>Pessoa</th><th>Descrição</th></tr></thead>
    <tbody></tbody>
  </table>
  </div>

  <details class="adv" style="margin-top:1rem">
    <summary>Log técnico da execução atual</summary>
    <pre id="log"></pre>
  </details>
</section>

</main>

<script>
"use strict";
let mappingLoaded = false;
let wasRunning = false;
let importLog = [];

function el(tag, attrs = {}, text = "") {
  const node = document.createElement(tag);
  Object.assign(node, attrs);
  if (text) node.textContent = text;
  return node;
}
function show(id, text, cls) {
  const n = document.getElementById(id);
  n.textContent = text;
  n.className = cls || "";
}

/* ---------- conexões ---------- */
const CFG_KEYS = ["ODOO_URL","ODOO_DB","ODOO_USER","ODOO_API_KEY",
                  "JIRA_URL","JIRA_USER","JIRA_API_TOKEN"];
const CFG_SECRETS = ["ODOO_API_KEY","JIRA_API_TOKEN"];

async function loadConfig() {
  const data = await (await fetch("/api/config")).json();
  for (const key of CFG_KEYS) {
    const input = document.getElementById("cfg_" + key);
    const field = data.fields[key] || {};
    if (CFG_SECRETS.includes(key)) {
      input.placeholder = field.set ? "•••• já configurada — deixe em branco para manter"
                                    : "cole aqui";
    } else if (field.value) {
      input.value = field.value;
    }
  }
  applyConfigured(data.configured);
}
function applyConfigured(configured) {
  document.getElementById("cfgState").style.display = configured ? "none" : "";
  document.getElementById("btnSync").disabled = !configured;
  document.getElementById("btnDry").disabled = !configured;
  if (!configured) document.getElementById("cfgDetails").open = true;
}
async function saveConfig() {
  const body = {};
  for (const key of CFG_KEYS) {
    const value = document.getElementById("cfg_" + key).value.trim();
    if (value) body[key] = value;
  }
  const res = await fetch("/api/config", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) {
    show("cfgMsg", "Salvo. Testando conexões…", "ok-text");
    for (const key of CFG_SECRETS) document.getElementById("cfg_" + key).value = "";
    applyConfigured(data.configured);
    await testConnection();
  } else {
    show("cfgMsg", data.error || "não foi possível salvar", "err-text");
  }
}
async function testConnection() {
  show("cfgMsg", "Testando conexões…");
  const data = await (await fetch("/api/test", {method: "POST"})).json();
  show("cfgMsg", data.detail, data.ok ? "ok-text" : "err-text");
}

/* ---------- de-para ---------- */
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
function addProjectRow() { document.querySelector("#projTable tbody").appendChild(projectRow()); }
function addUserRow() { document.querySelector("#userTable tbody").appendChild(userRow()); }

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
async function saveMapping() {
  const res = await fetch("/api/mapping", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collectMapping()),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) show("mapMsg", "salvo ✓", "ok-text");
  else show("mapMsg", (data.errors || [data.error || "erro"]).join(" · "), "err-text");
}

/* ---------- sincronizar ---------- */
async function runSync(dryRun) {
  const res = await fetch("/api/sync", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      dry_run: dryRun,
      since: document.getElementById("since").value || null,
      delete: document.getElementById("propDelete").checked,
    }),
  });
  const data = await res.json().catch(() => ({}));
  show("msg", res.ok && data.ok
    ? (dryRun ? "simulação iniciada…" : "sincronização iniciada…")
    : (data.error || "não foi possível iniciar"), res.ok ? "" : "warn-text");
}

function renderHistory(history) {
  const body = document.querySelector("#histTable tbody");
  body.innerHTML = "";
  for (const run of history) {
    const tr = el("tr");
    tr.appendChild(el("td", {}, run.finished_utc ? new Date(run.finished_utc).toLocaleString() : "—"));
    tr.appendChild(el("td", {}, run.dry_run ? "simulação" : "real"));
    tr.appendChild(el("td", {}, run.since ? new Date(run.since).toLocaleDateString() : "—"));
    for (const field of ["created", "updated", "skipped", "deleted"])
      tr.appendChild(el("td", {}, String(run[field] ?? "—")));
    tr.appendChild(el("td", {}, String((run.warnings || []).length)));
    const result = el("td", {}, run.ok ? "✓ ok" : "✕ " + (run.error || "erro"));
    result.className = run.ok ? "ok-text" : "err-text";
    tr.appendChild(result);
    body.appendChild(tr);
  }
  const last = history[0];
  const line = document.getElementById("lastRunLine");
  const cards = document.getElementById("statCards");
  const warnBox = document.getElementById("warnBox");
  if (!last) { warnBox.style.display = "none"; return; }
  cards.style.display = "";
  document.getElementById("stCreated").textContent = last.created ?? "–";
  document.getElementById("stUpdated").textContent = last.updated ?? "–";
  document.getElementById("stWarnings").textContent = (last.warnings || []).length;
  const quando = last.finished_utc ? new Date(last.finished_utc).toLocaleString() : "";
  if (last.ok) {
    line.className = "";
    line.textContent = "Última execução (" + (last.dry_run ? "simulação" : "real") + "): "
      + quando + ".";
  } else {
    line.className = "err";
    line.textContent = "A última execução terminou com erro: " + (last.error || "?");
  }
  const warns = last.warnings || [];
  if (warns.length) {
    warnBox.style.display = "";
    warnBox.textContent = "Avisos:\n• " + warns.join("\n• ");
  } else {
    warnBox.style.display = "none";
  }
}

/* ---------- apontamentos importados ---------- */
async function loadImportLog() {
  try {
    const res = await fetch("/api/import-log?limit=500");
    importLog = (await res.json()).items || [];
    renderImportLog();
  } catch (e) { /* servidor ocupado */ }
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
    tr.appendChild(el("td", {}, it.logged_utc ? new Date(it.logged_utc).toLocaleString() : "—"));
    tr.appendChild(el("td", {}, it.acao || "—"));
    tr.appendChild(el("td", {}, it.issue || (it.worklog ? "worklog " + it.worklog : "—")));
    tr.appendChild(el("td", {}, it.data || "—"));
    tr.appendChild(el("td", {}, it.horas != null ? String(it.horas) : "—"));
    tr.appendChild(el("td", {}, it.autor || "—"));
    tr.appendChild(el("td", {}, it.descricao || "—"));
    body.appendChild(tr);
  }
}

/* ---------- ciclo ---------- */
async function refresh() {
  try {
    const state = await (await fetch("/api/state")).json();
    const status = document.getElementById("status");
    if (state.running) { status.className = "pill warn"; status.textContent = "⏳ sincronizando…"; }
    else { status.className = "pill ok"; status.textContent = "pronto"; }
    applyConfigured(!!state.configured && !state.running ? true : !!state.configured);
    if (state.running) {
      document.getElementById("btnSync").disabled = true;
      document.getElementById("btnDry").disabled = true;
    }
    if (!mappingLoaded) { renderMapping(state.mapping); mappingLoaded = true; }
    renderHistory(state.history);
    document.getElementById("log").textContent = state.log.join("\n");
    if (wasRunning && !state.running) loadImportLog();
    wasRunning = state.running;
  } catch (e) { /* servidor reiniciando */ }
}

document.getElementById("btnSync").addEventListener("click", () => runSync(false));
document.getElementById("btnDry").addEventListener("click", () => runSync(true));
document.getElementById("btnSaveCfg").addEventListener("click", saveConfig);
document.getElementById("btnTest").addEventListener("click", testConnection);
document.getElementById("btnSaveMapping").addEventListener("click", saveMapping);
document.getElementById("implogFilter").addEventListener("input", renderImportLog);

loadConfig();
loadImportLog();
refresh();
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
    parser.add_argument("--log-file", default=DEFAULT_RUN_LOG_FILE)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--no-browser", action="store_true", help="não abrir o navegador automaticamente"
    )
    args = parser.parse_args(argv)

    configure_logging(log_file=args.log_file or None)
    load_env_file(args.env_file)
    runner = SyncRunner(
        Path(args.mapping_file),
        Path(args.state_file),
        Path(args.history_file),
        Path(args.import_log_file),
        Path(args.env_file),
    )
    server = App((args.host, args.port), runner)
    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"Aplicativo disponível em {url}")
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
