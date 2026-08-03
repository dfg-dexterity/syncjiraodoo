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

import time
import webbrowser
from http.cookies import SimpleCookie

from . import auth, storage
from .config import Config, ConfigError
from .envfile import DEFAULT_ENV_FILE, load_env_file, save_env_file
from .github_client import DEFAULT_BUG_REPO, GitHubError, create_issue
from .jira_client import JiraClient
from .logsetup import DEFAULT_RUN_LOG_FILE, configure_logging
from .mapping import Mapping
from .odoo_client import OdooClient
from .scheduler import (
    DEFAULT_SCHEDULE_FILE,
    Scheduler,
    load_schedule,
    next_due,
    save_schedule,
    validate_schedule,
)
from .sync import SyncEngine, connection_report

# variáveis gerenciadas pela tela de configuração; as marcadas são segredos
# e nunca voltam para o navegador (só um "está configurada")
CONFIG_KEYS = (
    "ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_API_KEY",
    "JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN", "DEFAULT_EMPLOYEE_EMAIL",
    "GITHUB_TOKEN", "GITHUB_REPO",
)
SECRET_KEYS = {"ODOO_API_KEY", "JIRA_API_TOKEN", "GITHUB_TOKEN"}
OPTIONAL_KEYS = {"DEFAULT_EMPLOYEE_EMAIL", "GITHUB_TOKEN", "GITHUB_REPO"}
REQUIRED_KEYS = tuple(k for k in CONFIG_KEYS if k not in OPTIONAL_KEYS)

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
        self.schedule_path = Path(DEFAULT_SCHEDULE_FILE)
        self.lock = threading.Lock()
        self.running = False
        # resultado da última conferência Jira × Odoo (memória do processo)
        self.audit: dict | None = None
        self.log_buffer: collections.deque[str] = collections.deque(maxlen=400)
        log.addHandler(_BufferHandler(self.log_buffer))

    def _acquire(self) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
        self.log_buffer.clear()
        return True

    def _release(self) -> None:
        with self.lock:
            self.running = False

    def _build_engine(self) -> SyncEngine:
        cfg = Config.from_env()
        mapping = Mapping.load(self.mapping_path)
        errors = mapping.validate()
        if errors:
            raise ConfigError("mapping inválido: " + "; ".join(errors))
        cfg.apply_mapping(mapping)
        odoo = OdooClient(cfg.odoo_url, cfg.odoo_db, cfg.odoo_user, cfg.odoo_api_key)
        jira = JiraClient(cfg.jira_url, cfg.jira_user, cfg.jira_api_token)
        return SyncEngine(jira, odoo, cfg)

    def start(
        self,
        dry_run: bool,
        since: str | None,
        delete: bool,
        scheduled: bool = False,
        close_tasks: bool = False,
    ) -> bool:
        if not self._acquire():
            return False
        threading.Thread(
            target=self._execute,
            args=(dry_run, since, delete, scheduled, close_tasks),
            daemon=True,
        ).start()
        return True

    def start_close_tasks(self) -> bool:
        if not self._acquire():
            return False
        threading.Thread(target=self._execute_close, daemon=True).start()
        return True

    def _execute_close(self) -> None:
        record: dict = {"finished_utc": None, "dry_run": False, "close_only": True, "ok": False}
        try:
            engine = self._build_engine()
            result = engine.close_done_tasks(dry_run=False)
            record.update(
                created=0,
                updated=result.updated,
                skipped=0,
                deleted=0,
                warnings=result.warnings,
                ok=True,
            )
            storage.append_import_items(self.import_log_path, result.items)
        except Exception as exc:
            log.error("conclusão de tarefas falhou: %s", exc)
            record["error"] = str(exc)
        finally:
            record["finished_utc"] = datetime.now(timezone.utc).isoformat()
            storage.append_history(self.history_path, record)
            self._release()

    def start_audit(self, since_str: str) -> bool:
        if not self._acquire():
            return False
        threading.Thread(target=self._execute_audit, args=(since_str,), daemon=True).start()
        return True

    def start_reimport(self, worklog_ids: list) -> bool:
        if not self._acquire():
            return False
        threading.Thread(
            target=self._execute_reimport, args=(worklog_ids,), daemon=True
        ).start()
        return True

    def _execute_audit(self, since_str: str) -> None:
        audit: dict = {"since": since_str, "finished_utc": None, "items": [], "error": None}
        try:
            engine = self._build_engine()
            since = storage.load_since(since_str, self.state_path)
            audit["since"] = since.isoformat()
            audit["items"] = engine.audit(since)
            counts = collections.Counter(item["status"] for item in audit["items"])
            log.info(
                "conferência concluída: %d ok, %d divergentes, %d faltando, %d duplicados",
                counts.get("ok", 0),
                counts.get("divergente", 0),
                counts.get("faltando", 0),
                counts.get("duplicado", 0),
            )
        except Exception as exc:
            log.error("conferência falhou: %s", exc)
            audit["error"] = str(exc)
        finally:
            audit["finished_utc"] = datetime.now(timezone.utc).isoformat()
            self.audit = audit
            self._release()

    def _execute_reimport(self, worklog_ids: list) -> None:
        record: dict = {"finished_utc": None, "dry_run": False, "reimport": True, "ok": False}
        try:
            engine = self._build_engine()
            result = engine.resync(worklog_ids)
            log.info(
                "reimportação: %d criados, %d atualizados, %d pulados, %d avisos",
                result.created, result.updated, result.skipped, len(result.warnings),
            )
            record.update(
                created=result.created,
                updated=result.updated,
                skipped=result.skipped,
                deleted=0,
                warnings=result.warnings,
                ok=True,
            )
            storage.append_import_items(self.import_log_path, result.items)
        except Exception as exc:
            log.error("reimportação falhou: %s", exc)
            record["error"] = str(exc)
        finally:
            record["finished_utc"] = datetime.now(timezone.utc).isoformat()
            storage.append_history(self.history_path, record)
            self._release()

    def _execute(
        self,
        dry_run: bool,
        since_str: str | None,
        delete: bool,
        scheduled: bool = False,
        close_tasks: bool = False,
    ) -> None:
        record: dict = {
            "finished_utc": None,
            "dry_run": dry_run,
            "delete": delete,
            "scheduled": scheduled,
            "ok": False,
        }
        try:
            engine = self._build_engine()
            since = storage.load_since(since_str or None, self.state_path)
            run_started = datetime.now(timezone.utc)
            result = engine.run(since, dry_run=dry_run, delete=delete)
            if close_tasks:
                close_result = engine.close_done_tasks(dry_run=dry_run)
                record["tasks_closed"] = close_result.updated
                result.warnings.extend(close_result.warnings)
                result.items.extend(close_result.items)

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
    def __init__(
        self,
        address: tuple[str, int],
        runner: SyncRunner,
        users_path: Path | None = None,
    ):
        super().__init__(address, Handler)
        self.runner = runner
        self.users_path = users_path or Path(auth.DEFAULT_USERS_FILE)
        self.sessions = auth.SessionStore()


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

    # -------------------------------------------------------------- sessão

    def _session_token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(auth.SESSION_COOKIE)
        return morsel.value if morsel else None

    def _current_user(self) -> dict | None:
        email = self.server.sessions.get(self._session_token())
        if not email:
            return None
        record = auth.load_users(self.server.users_path).get(email)
        if record is None:
            return None
        return {"email": email, "admin": bool(record.get("admin"))}

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        runner = self.server.runner
        user = self._current_user()
        if self.path == "/":
            self._send_html(INDEX_HTML if user else LOGIN_HTML)
            return
        if self.path == "/api/auth-state":
            self._send_json(
                {
                    "needs_setup": not auth.load_users(self.server.users_path),
                    "logged_in": user is not None,
                    "email": user["email"] if user else "",
                    "admin": bool(user and user["admin"]),
                }
            )
            return
        if user is None:
            self._send_json({"error": "faça login para continuar"}, status=401)
            return
        if self.path == "/api/schedule":
            schedule = load_schedule(runner.schedule_path)
            due = next_due(schedule, datetime.now(timezone.utc))
            schedule.pop("last_started_utc", None)
            self._send_json({"schedule": schedule, "next_utc": due.isoformat() if due else None})
            return
        if self.path == "/api/audit":
            self._send_json({"running": runner.running, "audit": runner.audit})
            return
        if self.path == "/api/users":
            if not user["admin"]:
                self._send_json({"error": "somente administradores"}, status=403)
                return
            users = auth.load_users(self.server.users_path)
            self._send_json(
                {
                    "users": [
                        {"email": email, "admin": bool(record.get("admin"))}
                        for email, record in sorted(users.items())
                    ]
                }
            )
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
                    "user": user,
                    "mapping": {
                        "restrict_to_mapped_projects": mapping.restrict_to_mapped_projects,
                        "projects": mapping.projects,
                        "users": mapping.users,
                        "department_routing": mapping.department_routing,
                    },
                    "history": list(reversed(history[-50:])),
                    "log": list(runner.log_buffer),
                }
            )
        else:
            self._send_json({"error": "não encontrado"}, status=404)

    def _set_session_cookie(self, token: str, expire: bool = False) -> None:
        attrs = f"{auth.SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            # atrás de um proxy HTTPS (Caddy etc.), o cookie nunca viaja em claro
            attrs += "; Secure"
        if expire:
            attrs += "; Max-Age=0"
        self.send_header("Set-Cookie", attrs)

    def _login_ok(self, email: str, admin: bool) -> None:
        token = self.server.sessions.create(email)
        body = json.dumps({"ok": True, "admin": admin}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._set_session_cookie(token)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        runner = self.server.runner
        if self.path == "/api/login":
            data = self._read_json()
            email = str(data.get("email", "")).strip().lower()
            record = auth.verify_user(self.server.users_path, email, str(data.get("senha", "")))
            if record is None:
                time.sleep(0.4)  # desestimula tentativa e erro
                self._send_json({"ok": False, "error": "e-mail ou senha incorretos"}, 401)
                return
            self._login_ok(email, bool(record.get("admin")))
            return
        if self.path == "/api/setup":
            if auth.load_users(self.server.users_path):
                self._send_json({"ok": False, "error": "o aplicativo já tem usuários"}, 403)
                return
            data = self._read_json()
            try:
                auth.add_user(
                    self.server.users_path,
                    str(data.get("email", "")),
                    str(data.get("senha", "")),
                    admin=True,
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
                return
            self._login_ok(str(data.get("email", "")).strip().lower(), True)
            return

        user = self._current_user()
        if user is None:
            self._send_json({"error": "faça login para continuar"}, status=401)
            return
        if self.path == "/api/logout":
            self.server.sessions.drop(self._session_token())
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._set_session_cookie("saiu", expire=True)
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/schedule":
            data = self._read_json()
            schedule, error = validate_schedule(data)
            if schedule is None:
                self._send_json({"ok": False, "error": error}, 400)
                return
            # preserva o último disparo para não duplicar execuções
            previous = load_schedule(runner.schedule_path)
            if previous.get("last_started_utc"):
                schedule["last_started_utc"] = previous["last_started_utc"]
            save_schedule(runner.schedule_path, schedule)
            log.info("agenda de sincronização alterada por %s: %s", user["email"], schedule)
            due = next_due(schedule, datetime.now(timezone.utc))
            self._send_json({"ok": True, "next_utc": due.isoformat() if due else None})
            return
        if self.path == "/api/audit":
            data = self._read_json()
            since = str(data.get("since", "")).strip()
            if not since:
                self._send_json({"ok": False, "error": "informe a data inicial"}, 400)
                return
            if not runner.start_audit(since):
                self._send_json({"ok": False, "error": "já existe uma execução em andamento"}, 409)
                return
            self._send_json({"ok": True})
            return
        if self.path == "/api/reimport":
            data = self._read_json()
            worklogs = [str(w).strip() for w in data.get("worklogs", []) if str(w).strip()]
            if not worklogs:
                self._send_json({"ok": False, "error": "selecione ao menos um item"}, 400)
                return
            if not all(w.isdigit() for w in worklogs):
                self._send_json({"ok": False, "error": "ids de worklog inválidos"}, 400)
                return
            if not runner.start_reimport(worklogs):
                self._send_json({"ok": False, "error": "já existe uma execução em andamento"}, 409)
                return
            log.info("reimportação solicitada por %s: %s", user["email"], worklogs)
            self._send_json({"ok": True})
            return
        if self.path == "/api/bug":
            data = self._read_json()
            titulo = str(data.get("titulo", "")).strip()
            descricao = str(data.get("descricao", "")).strip()
            if not titulo or not descricao:
                self._send_json({"ok": False, "error": "preencha o título e a descrição"}, 400)
                return
            token = os.environ.get("GITHUB_TOKEN", "").strip()
            if not token:
                self._send_json(
                    {
                        "ok": False,
                        "error": "reporte de bugs não configurado: informe o token do "
                        "GitHub na seção Conexões",
                    },
                    400,
                )
                return
            repo = os.environ.get("GITHUB_REPO", "").strip() or DEFAULT_BUG_REPO
            corpo = [
                f"**Reportado por:** {user['email']} (pelo aplicativo Sincronizador de Horas)",
                "",
                "## Descrição",
                "",
                descricao,
            ]
            history = storage.read_history(runner.history_path)
            if history:
                last = history[0]
                resultado = "ok" if last.get("ok") else f"erro: {last.get('error', '?')}"
                corpo += [
                    "",
                    "---",
                    f"**Última execução:** {last.get('finished_utc', '?')} — {resultado} "
                    f"({'simulação' if last.get('dry_run') else 'real'}; "
                    f"{last.get('created', 0)} criados, {last.get('updated', 0)} atualizados, "
                    f"{len(last.get('warnings', []))} avisos)",
                ]
            try:
                issue_url = create_issue(repo, token, f"[bug] {titulo}", "\n".join(corpo))
            except GitHubError as exc:
                self._send_json({"ok": False, "error": str(exc)}, 502)
                return
            log.info("bug reportado por %s: %s", user["email"], issue_url)
            self._send_json({"ok": True, "url": issue_url})
            return
        if self.path in ("/api/users", "/api/users/delete"):
            if not user["admin"]:
                self._send_json({"error": "somente administradores"}, status=403)
                return
            data = self._read_json()
            try:
                if self.path == "/api/users":
                    auth.add_user(
                        self.server.users_path,
                        str(data.get("email", "")),
                        str(data.get("senha", "")),
                        admin=bool(data.get("admin")),
                    )
                else:
                    auth.remove_user(self.server.users_path, str(data.get("email", "")))
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
                return
            self._send_json({"ok": True})
            return
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
                department_routing=dict(data.get("department_routing", {})),
            )
            errors = mapping.validate()
            if errors:
                self._send_json({"ok": False, "errors": errors}, status=400)
                return
            mapping.save(runner.mapping_path)
            self._send_json({"ok": True})
        elif self.path == "/api/close-tasks":
            if runner.start_close_tasks():
                log.info("conclusão de tarefas solicitada por %s", user["email"])
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": "já existe uma execução em andamento"}, 409)
        elif self.path == "/api/sync":
            data = self._read_json()
            started = runner.start(
                dry_run=bool(data.get("dry_run")),
                since=data.get("since") or None,
                delete=bool(data.get("delete")),
                close_tasks=bool(data.get("close_tasks")),
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


LOGIN_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrar — Sincronizador de Horas</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #f6f7f9; color: #1b2029;
    font: 15px/1.5 ui-sans-serif, system-ui, "Segoe UI", Roboto, Arial, sans-serif;
  }
  .box {
    width: min(400px, 92vw); background: #fff; border: 1px solid #dfe3ea;
    border-radius: 16px; padding: 2rem;
    box-shadow: 0 1px 2px rgba(27,32,41,.06), 0 8px 28px rgba(27,32,41,.08);
  }
  .logo { font-size: 2rem; text-align: center; }
  h1 { font-size: 1.1rem; text-align: center; margin: .4rem 0 .2rem; }
  p.sub { font-size: .82rem; color: #5d6674; text-align: center; margin: 0 0 1.2rem; }
  label { display: block; font-size: .78rem; font-weight: 600; color: #5d6674; margin: .8rem 0 .2rem; }
  input {
    width: 100%; font: inherit; padding: .55rem .7rem;
    border: 1px solid #c3c9d4; border-radius: 9px;
  }
  input:focus-visible { outline: 2px solid #1257b8; outline-offset: 1px; }
  button {
    width: 100%; margin-top: 1.2rem; font: inherit; font-weight: 700; cursor: pointer;
    padding: .7rem; border-radius: 10px; border: none; background: #1b2029; color: #fff;
  }
  button:hover { opacity: .9; }
  #err { color: #ab3226; font-size: .84rem; margin-top: .8rem; text-align: center; min-height: 1.2em; }
  #confirmWrap { display: none; }
</style>
</head>
<body>
<form class="box" id="form">
  <div class="logo">⏱</div>
  <h1 id="title">Entrar</h1>
  <p class="sub" id="subtitle">Sincronizador de Horas — Jira → Odoo</p>
  <label>E-mail</label>
  <input id="email" type="email" autocomplete="username" required autofocus>
  <label>Senha</label>
  <input id="senha" type="password" autocomplete="current-password" required minlength="8">
  <div id="confirmWrap">
    <label>Confirme a senha</label>
    <input id="confirma" type="password" autocomplete="new-password" minlength="8">
  </div>
  <label style="display:flex; gap:.4rem; align-items:center; font-size:.8rem; color:#5d6674; margin-top:.7rem">
    <input type="checkbox" id="verSenha" style="width:auto"> mostrar senhas
  </label>
  <button type="submit" id="btn">Entrar</button>
  <div id="err"></div>
</form>
<script>
"use strict";
let needsSetup = false;
fetch("/api/auth-state").then(r => r.json()).then(data => {
  needsSetup = !!data.needs_setup;
  if (needsSetup) {
    document.getElementById("title").textContent = "Bem-vindo! Crie o primeiro acesso";
    document.getElementById("subtitle").textContent =
      "Este será o usuário administrador do Sincronizador de Horas.";
    document.getElementById("confirmWrap").style.display = "";
    document.getElementById("btn").textContent = "Criar e entrar";
    // impede o navegador de sobrescrever o campo com senha gerada/salva
    const senhaInput = document.getElementById("senha");
    senhaInput.setAttribute("autocomplete", "new-password");
    senhaInput.value = "";
  }
});
document.getElementById("verSenha").addEventListener("change", event => {
  const type = event.target.checked ? "text" : "password";
  document.getElementById("senha").type = type;
  document.getElementById("confirma").type = type;
});
document.getElementById("form").addEventListener("submit", async event => {
  event.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  const email = document.getElementById("email").value.trim();
  const senha = document.getElementById("senha").value;
  if (needsSetup && senha !== document.getElementById("confirma").value) {
    err.textContent = "as senhas não conferem";
    return;
  }
  const res = await fetch(needsSetup ? "/api/setup" : "/api/login", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ email, senha }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) location.reload();
  else err.textContent = data.error || "não foi possível entrar";
});
</script>
</body>
</html>
"""

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

  .tabs {
    position: sticky; top: 0; z-index: 10;
    background: var(--surface); border-bottom: 1px solid var(--line);
  }
  .tabs-inner {
    max-width: 1080px; margin: 0 auto; padding: .45rem 1.25rem;
    display: flex; gap: .35rem; flex-wrap: wrap;
  }
  .tab {
    font-size: .88rem; font-weight: 600; border: none; background: none;
    color: var(--muted); padding: .45rem .85rem; border-radius: 9px; cursor: pointer;
  }
  .tab:hover { background: var(--surface-2); color: var(--ink); }
  .tab.active { background: var(--ink); color: var(--bg); }
  section[data-page] { display: none; }

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
  .field input, .field textarea {
    width: 100%; font: inherit; font-size: .88rem; padding: .45rem .6rem;
    border: 1px solid var(--line-strong); border-radius: 8px;
    background: var(--surface); color: var(--ink); resize: vertical;
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
  <span id="whoami" class="muted"></span>
  <button class="mini" id="btnBug" title="algo deu errado? conte para a TI">🐞 Reportar problema</button>
  <button class="mini" id="btnLogout" style="display:none">sair</button>
  <span id="status" class="pill ok">…</span>
</div></div>

<nav class="tabs"><div class="tabs-inner">
  <button class="tab" data-page="sync">▶ Sincronizar</button>
  <button class="tab" data-page="audit">🔍 Conferência</button>
  <button class="tab" data-page="mapping">🗺️ De-para</button>
  <button class="tab" data-page="activity">📋 Atividade</button>
  <button class="tab" data-page="settings">⚙️ Configurações</button>
</div></nav>

<main>

<!-- ============ REPORTAR BUG ============ -->
<section class="card" id="bugCard" style="display:none">
  <h2>🐞 Reportar um problema</h2>
  <p class="sub">Descreva o que aconteceu. O relato vira um chamado para a equipe de TI
  (issue no GitHub), já com o seu e-mail e o resumo da última execução anexados.</p>
  <div class="field"><label>Título curto</label>
    <input id="bugTitle" placeholder="ex.: horas do projeto Fiagril não apareceram no Odoo"></div>
  <div class="field"><label>O que aconteceu?</label>
    <textarea id="bugDesc" rows="5"
      placeholder="conte o que você estava fazendo, o que esperava e o que apareceu (inclua issue/projeto/data se souber)…"></textarea></div>
  <div class="actions" style="margin-top:.8rem">
    <button id="btnBugSend">Enviar relato</button>
    <button class="mini" id="btnBugCancel">cancelar</button>
    <span id="bugMsg" style="font-size:.85rem"></span>
  </div>
</section>

<!-- ============ SINCRONIZAR ============ -->
<section class="card" data-page="sync">
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
    <button id="btnCloseTasks" title="marca como concluídas no Odoo as tarefas cujas issues já foram finalizadas no Jira">✅ Concluir tarefas</button>
    <span id="msg"></span>
  </div>
  <div id="warnBox"></div>
  <details class="adv">
    <summary>Opções avançadas</summary>
    <div class="inner">
      <label>buscar desde: <input type="date" id="since"></label>
      <label><input type="checkbox" id="propDelete"> apagar no Odoo os apontamentos excluídos no Jira</label>
      <label><input type="checkbox" id="closeTasks"> concluir no Odoo as tarefas finalizadas no Jira</label>
    </div>
  </details>

  <h3 style="font-size:.92rem; margin:1.2rem 0 .3rem">⏰ Sincronização automática</h3>
  <p class="sub" style="margin-bottom:.5rem">O aplicativo roda a sincronização sozinho na frequência
  que você escolher (traz só o que mudou; nunca em paralelo com uma execução manual).</p>
  <div class="actions">
    <select id="schedMode" style="font:inherit; font-size:.86rem; padding:.4rem .6rem; border:1px solid var(--line-strong); border-radius:8px">
      <option value="off">desligada</option>
      <option value="30">a cada 30 minutos</option>
      <option value="60">a cada 1 hora</option>
      <option value="240">a cada 4 horas</option>
      <option value="daily">uma vez por dia às…</option>
    </select>
    <input type="time" id="schedTime" value="06:00" style="display:none; font:inherit; font-size:.86rem; padding:.35rem .6rem; border:1px solid var(--line-strong); border-radius:8px">
    <span class="muted" id="schedTimeHint" style="display:none">(horário de Brasília)</span>
    <label style="font-size:.82rem"><input type="checkbox" id="schedClose"> concluir tarefas também</label>
    <button class="mini" id="btnSchedSave">salvar agenda</button>
    <span id="schedMsg" style="font-size:.85rem"></span>
  </div>
  <p id="schedNext" class="muted" style="font-size:.82rem; margin:.4rem 0 0"></p>
</section>

<!-- ============ CONEXÕES ============ -->
<section class="card" data-page="settings">
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
      <fieldset>
        <legend>GitHub — reporte de bugs (opcional)</legend>
        <div class="field"><label>Repositório</label>
          <input id="cfg_GITHUB_REPO" placeholder="dfg-dexterity/syncjiraodoo"></div>
        <div class="field"><label>Token <span class="muted">(github.com → Settings → Developer settings → Fine-grained tokens, permissão Issues: write)</span></label>
          <input id="cfg_GITHUB_TOKEN" type="password" autocomplete="off"></div>
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
<section class="card" data-page="mapping">
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

  <h2 style="margin-top:1.4rem">Roteamento por departamento</h2>
  <p class="sub">Para projetos como <b>Tarefas Avulsas</b> e <b>Tarefas Administrativas</b>, o projeto do
  Odoo não vem da key do Jira: vem do valor do campo da issue (ex.: "Departamento Dexterity").
  Preencha o campo, as keys desses projetos e a tabela departamento → projeto Odoo.</p>
  <div class="actions" style="margin-bottom:.5rem">
    <label style="font-size:.85rem">Campo na issue do Jira:
      <input type="text" id="deptField" placeholder="Departamento Dexterity"
             style="font:inherit; font-size:.85rem; padding:.35rem .6rem; border:1px solid var(--line-strong); border-radius:8px; min-width:220px"></label>
    <label style="font-size:.85rem">Projetos Jira com roteamento (keys):
      <input type="text" id="deptProjects" placeholder="TAD, RDF"
             style="font:inherit; font-size:.85rem; padding:.35rem .6rem; border:1px solid var(--line-strong); border-radius:8px; min-width:160px"></label>
  </div>
  <label style="font-size:.85rem; display:block; margin-bottom:.5rem">
    <input type="checkbox" id="deptConcat">
    montar o projeto Odoo automaticamente como <b>"Projeto Jira | Departamento"</b>
    (ex.: <span class="muted">ITPR | Tarefas Avulsas | FI - Financeiro</span>) —
    a tabela abaixo vira só exceções
  </label>
  <div class="table-wrap">
  <table id="deptTable">
    <thead><tr><th>Departamento (valor exato no Jira)</th>
    <th>Projeto no Odoo (nome exato)</th>
    <th style="width:2rem"></th></tr></thead>
    <tbody></tbody>
  </table>
  </div>
  <button class="mini" onclick="addDeptRow()">+ adicionar departamento</button>

  <div class="actions" style="margin-top:.8rem">
    <button id="btnSaveMapping">💾 Salvar de-para</button>
    <span id="mapMsg" style="font-size:.85rem"></span>
  </div>
</section>

<!-- ============ EQUIPE ============ -->
<section class="card" id="teamCard" data-page="settings">
  <h2>Equipe</h2>
  <p class="sub">Quem pode entrar neste aplicativo. Somente administradores veem esta seção.</p>
  <div class="table-wrap">
  <table id="usersTable">
    <thead><tr><th>E-mail</th><th>Perfil</th><th style="width:2rem"></th></tr></thead>
    <tbody></tbody>
  </table>
  </div>
  <div class="actions" style="margin-top:.6rem">
    <input id="tm_email" type="email" placeholder="email@empresa.com.br"
           style="font:inherit; font-size:.85rem; padding:.4rem .6rem; border:1px solid var(--line-strong); border-radius:8px">
    <input id="tm_senha" type="password" placeholder="senha inicial (mín. 8)"
           style="font:inherit; font-size:.85rem; padding:.4rem .6rem; border:1px solid var(--line-strong); border-radius:8px">
    <label style="font-size:.82rem"><input type="checkbox" id="tm_admin"> administrador</label>
    <button class="mini" id="btnTeamAdd">+ adicionar pessoa</button>
    <span id="teamMsg" style="font-size:.82rem"></span>
  </div>
</section>

<!-- ============ CONFERÊNCIA ============ -->
<section class="card" data-page="audit">
  <h2>Conferência Jira × Odoo</h2>
  <p class="sub">Compara item a item o que está no Jira com o que foi gravado no Odoo
  (data, horas, descrição) — <b>sem alterar nada</b>. O que estiver diferente ou
  faltando pode ser reimportado com um clique.</p>
  <div class="actions">
    <label style="font-size:.86rem">conferir desde: <input type="date" id="auditSince"></label>
    <button id="btnAudit">🔍 Conferir</button>
    <span id="auditMsg" style="font-size:.85rem"></span>
  </div>
  <div class="summary" id="auditStats" style="display:none; margin-top:.9rem">
    <div class="stat"><b id="auOk">–</b><span>✓ iguais</span></div>
    <div class="stat"><b id="auDiff">–</b><span>≠ divergentes</span></div>
    <div class="stat"><b id="auMissing">–</b><span>faltando no Odoo</span></div>
  </div>
  <div class="table-wrap" id="auditWrap" style="display:none">
  <table id="auditTable">
    <thead><tr><th style="width:2rem"><input type="checkbox" id="auAll" title="marcar todos os não-ok"></th>
    <th>Issue</th><th>Pessoa</th><th>Data</th><th>Horas (Jira)</th><th>Horas (Odoo)</th>
    <th>Situação</th></tr></thead>
    <tbody></tbody>
  </table>
  </div>
  <div class="actions" id="auditActions" style="display:none; margin-top:.8rem">
    <button id="btnReimport">↻ Reimportar selecionados</button>
    <label style="font-size:.82rem"><input type="checkbox" id="auOnlyProblems" checked> mostrar só divergentes/faltando</label>
    <span id="reimportMsg" style="font-size:.85rem"></span>
  </div>
</section>

<!-- ============ ATIVIDADE ============ -->
<section class="card" data-page="activity">
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
let currentPage = "sync";

/* ---------- navegação por abas ---------- */
const PAGES = ["sync", "audit", "mapping", "activity", "settings"];

function renderPages() {
  for (const section of document.querySelectorAll("section[data-page]")) {
    let visible = section.dataset.page === currentPage;
    if (section.id === "teamCard" && !(me && me.admin)) visible = false;
    section.style.display = visible ? "block" : "none";
  }
  for (const tab of document.querySelectorAll(".tab"))
    tab.classList.toggle("active", tab.dataset.page === currentPage);
}

function showPage(name) {
  if (!PAGES.includes(name)) name = "sync";
  currentPage = name;
  history.replaceState(null, "", "#" + name);
  renderPages();
  window.scrollTo({ top: 0 });
}

document.querySelectorAll(".tab").forEach(tab =>
  tab.addEventListener("click", () => showPage(tab.dataset.page)));

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
                  "JIRA_URL","JIRA_USER","JIRA_API_TOKEN",
                  "GITHUB_REPO","GITHUB_TOKEN"];
const CFG_SECRETS = ["ODOO_API_KEY","JIRA_API_TOKEN","GITHUB_TOKEN"];

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
let onboardingShown = false;
function applyConfigured(configured) {
  document.getElementById("cfgState").style.display = configured ? "none" : "";
  document.getElementById("btnSync").disabled = !configured;
  document.getElementById("btnDry").disabled = !configured;
  if (!configured) {
    document.getElementById("cfgDetails").open = true;
    if (!onboardingShown) { onboardingShown = true; showPage("settings"); }
  }
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
function deptRow(departamento = "", odoo = "") {
  const tr = el("tr");
  for (const value of [departamento, odoo]) {
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
function addDeptRow() { document.querySelector("#deptTable tbody").appendChild(deptRow()); }

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
  const routing = mapping.department_routing || {};
  document.getElementById("deptField").value = routing.field || "";
  document.getElementById("deptProjects").value = (routing.projects || []).join(", ");
  document.getElementById("deptConcat").checked = !!routing.concat;
  const deptBody = document.querySelector("#deptTable tbody");
  deptBody.innerHTML = "";
  for (const row of routing.map || [])
    deptBody.appendChild(deptRow(row.departamento || "", row.odoo || ""));
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
  const deptMap = [...document.querySelectorAll("#deptTable tbody tr")].map(tr => {
    const [departamento, odoo] = [...tr.querySelectorAll("input")].map(i => i.value.trim());
    return { departamento, odoo };
  }).filter(r => r.departamento || r.odoo);
  const deptField = document.getElementById("deptField").value.trim();
  const deptProjects = document.getElementById("deptProjects").value
    .split(",").map(s => s.trim().toUpperCase()).filter(Boolean);
  const deptConcat = document.getElementById("deptConcat").checked;
  const department_routing = (deptField || deptProjects.length || deptMap.length || deptConcat)
    ? { field: deptField, projects: deptProjects, map: deptMap, concat: deptConcat }
    : {};
  return {
    restrict_to_mapped_projects: document.getElementById("restrict").checked,
    projects, users, department_routing,
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
      close_tasks: document.getElementById("closeTasks").checked,
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
    tr.appendChild(el("td", {}, run.close_only ? "conclusão de tarefas"
      : run.reimport ? "reimportação"
      : run.scheduled ? "automática" : run.dry_run ? "simulação" : "real"));
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

/* ---------- sincronização automática ---------- */
function renderScheduleNext(nextUtc) {
  const line = document.getElementById("schedNext");
  line.textContent = nextUtc
    ? "próxima execução automática: " + new Date(nextUtc).toLocaleString()
    : "sincronização automática desligada — rode manualmente quando quiser";
}
function applyScheduleForm(schedule) {
  const mode = !schedule.enabled ? "off"
    : schedule.daily_time ? "daily" : String(schedule.interval_minutes || 60);
  const select = document.getElementById("schedMode");
  if ([...select.options].some(o => o.value === mode)) select.value = mode;
  if (schedule.daily_time) document.getElementById("schedTime").value = schedule.daily_time;
  document.getElementById("schedClose").checked = !!schedule.close_tasks;
  toggleScheduleTime();
}
function toggleScheduleTime() {
  const daily = document.getElementById("schedMode").value === "daily";
  document.getElementById("schedTime").style.display = daily ? "" : "none";
  document.getElementById("schedTimeHint").style.display = daily ? "" : "none";
}
async function loadSchedule() {
  try {
    const data = await (await fetch("/api/schedule")).json();
    applyScheduleForm(data.schedule || {});
    renderScheduleNext(data.next_utc);
  } catch (e) { /* servidor ocupado */ }
}
document.getElementById("schedMode").addEventListener("change", toggleScheduleTime);
document.getElementById("btnSchedSave").addEventListener("click", async () => {
  const mode = document.getElementById("schedMode").value;
  const body = {
    enabled: mode !== "off", interval_minutes: 0, daily_time: "",
    close_tasks: document.getElementById("schedClose").checked,
  };
  if (mode === "daily") body.daily_time = document.getElementById("schedTime").value;
  else if (mode !== "off") body.interval_minutes = parseInt(mode, 10);
  const res = await fetch("/api/schedule", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) {
    show("schedMsg", "agenda salva ✓", "ok-text");
    renderScheduleNext(data.next_utc);
  } else show("schedMsg", data.error || "não foi possível salvar", "err-text");
});

/* ---------- conferência Jira × Odoo ---------- */
let auditWatch = false;
let reimportPending = null;
let auditItems = [];

function statusLabel(item) {
  if (item.status === "ok") return "✓ ok";
  if (item.status === "reimportado") return "↻ reimportado";
  if (item.status === "faltando") return "falta no Odoo";
  if (item.status === "duplicado") return "duplicado no Odoo";
  return "≠ " + (item.diferencas || []).join(", ");
}

function renderAudit(data) {
  const audit = data.audit;
  const msg = document.getElementById("auditMsg");
  if (!audit) return;
  if (audit.error) { show("auditMsg", audit.error, "err-text"); return; }
  auditItems = audit.items || [];
  const counts = { ok: 0, divergente: 0, faltando: 0, duplicado: 0 };
  for (const item of auditItems) counts[item.status] = (counts[item.status] || 0) + 1;
  document.getElementById("auditStats").style.display = "";
  document.getElementById("auOk").textContent = counts.ok;
  document.getElementById("auDiff").textContent = counts.divergente + counts.duplicado;
  document.getElementById("auMissing").textContent = counts.faltando;
  show("auditMsg", auditItems.length + " itens conferidos (desde " +
    new Date(audit.since).toLocaleDateString() + ")", "ok-text");
  document.getElementById("auditWrap").style.display = "";
  document.getElementById("auditActions").style.display = "";
  renderAuditRows();
}

function renderAuditRows() {
  const onlyProblems = document.getElementById("auOnlyProblems").checked;
  const body = document.querySelector("#auditTable tbody");
  body.innerHTML = "";
  for (const item of auditItems) {
    if (onlyProblems && item.status === "ok") continue;
    const tr = el("tr");
    const tdSel = el("td");
    if (item.status !== "ok" && item.status !== "duplicado" && item.status !== "reimportado") {
      tdSel.appendChild(el("input", { type: "checkbox", className: "auSel", value: String(item.worklog) }));
    }
    tr.appendChild(tdSel);
    tr.appendChild(el("td", {}, item.issue || "—"));
    tr.appendChild(el("td", {}, item.autor || "—"));
    tr.appendChild(el("td", {}, item.data_jira || "—"));
    tr.appendChild(el("td", {}, String(item.horas_jira ?? "—")));
    tr.appendChild(el("td", {}, item.horas_odoo != null ? String(item.horas_odoo) : "—"));
    const st = el("td", {}, statusLabel(item));
    st.className = (item.status === "ok" || item.status === "reimportado") ? "ok-text"
      : item.status === "divergente" ? "warn-text" : "err-text";
    tr.appendChild(st);
    body.appendChild(tr);
  }
}

async function loadAudit() {
  try {
    const data = await (await fetch("/api/audit")).json();
    if (!data.running) renderAudit(data);
    return data;
  } catch (e) { return null; }
}

document.getElementById("btnAudit").addEventListener("click", async () => {
  const since = document.getElementById("auditSince").value;
  if (!since) { show("auditMsg", "escolha a data inicial", "err-text"); return; }
  const res = await fetch("/api/audit", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ since }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) { auditWatch = true; show("auditMsg", "conferindo… (períodos longos podem demorar)", ""); }
  else show("auditMsg", data.error || "não foi possível iniciar", "err-text");
});

document.getElementById("auOnlyProblems").addEventListener("change", renderAuditRows);
document.getElementById("auAll").addEventListener("change", event => {
  for (const box of document.querySelectorAll(".auSel")) box.checked = event.target.checked;
});

document.getElementById("btnReimport").addEventListener("click", async () => {
  const ids = [...document.querySelectorAll(".auSel:checked")].map(b => b.value);
  if (!ids.length) { show("reimportMsg", "marque os itens que quer reimportar", "err-text"); return; }
  const res = await fetch("/api/reimport", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ worklogs: ids }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) {
    reimportPending = ids;
    show("reimportMsg", "reimportando " + ids.length + " item(ns)…", "");
  } else show("reimportMsg", data.error || "não foi possível reimportar", "err-text");
});

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

/* ---------- equipe ---------- */
let me = null;
let teamLoaded = false;

async function loadTeam() {
  const res = await fetch("/api/users");
  if (!res.ok) return;
  const data = await res.json();
  const body = document.querySelector("#usersTable tbody");
  body.innerHTML = "";
  for (const u of data.users || []) {
    const tr = el("tr");
    tr.appendChild(el("td", {}, u.email));
    tr.appendChild(el("td", {}, u.admin ? "administrador" : "membro"));
    const td = el("td");
    const del = el("button", { className: "del", title: "remover acesso" }, "✕");
    del.onclick = async () => {
      const r = await fetch("/api/users/delete", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ email: u.email }),
      });
      const d = await r.json().catch(() => ({}));
      show("teamMsg", r.ok ? "removido" : (d.error || "erro"), r.ok ? "ok-text" : "err-text");
      loadTeam();
    };
    td.appendChild(del);
    tr.appendChild(td);
    body.appendChild(tr);
  }
}

async function addTeamMember() {
  const email = document.getElementById("tm_email").value.trim();
  const senha = document.getElementById("tm_senha").value;
  const res = await fetch("/api/users", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ email, senha, admin: document.getElementById("tm_admin").checked }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) {
    show("teamMsg", "acesso criado — compartilhe a senha inicial com a pessoa", "ok-text");
    document.getElementById("tm_email").value = "";
    document.getElementById("tm_senha").value = "";
    document.getElementById("tm_admin").checked = false;
    loadTeam();
  } else {
    show("teamMsg", data.error || "não foi possível criar", "err-text");
  }
}

function applyUser(user) {
  me = user || null;
  document.getElementById("whoami").textContent = me ? me.email : "";
  document.getElementById("btnLogout").style.display = me ? "" : "none";
  renderPages();
  if (me && me.admin && !teamLoaded) { teamLoaded = true; loadTeam(); }
}

/* ---------- ciclo ---------- */
async function refresh() {
  try {
    const res = await fetch("/api/state");
    if (res.status === 401) { location.reload(); return; }
    const state = await res.json();
    applyUser(state.user);
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
    if (wasRunning && !state.running) {
      loadImportLog();
      if (auditWatch) { auditWatch = false; loadAudit(); }
      if (reimportPending) {
        // marca os itens reimportados na tabela; o botão Conferir confirma no Odoo
        for (const item of auditItems)
          if (reimportPending.includes(String(item.worklog))) item.status = "reimportado";
        reimportPending = null;
        renderAuditRows();
        show("reimportMsg", "reimportação concluída — clique em 🔍 Conferir para confirmar", "ok-text");
      }
    }
    wasRunning = state.running;
  } catch (e) { /* servidor reiniciando */ }
}

/* ---------- reportar problema ---------- */
document.getElementById("btnBug").addEventListener("click", () => {
  const card = document.getElementById("bugCard");
  const hidden = card.style.display === "none";
  card.style.display = hidden ? "" : "none";
  if (hidden) { card.scrollIntoView({ behavior: "smooth" }); document.getElementById("bugTitle").focus(); }
});
document.getElementById("btnBugCancel").addEventListener("click", () => {
  document.getElementById("bugCard").style.display = "none";
});
document.getElementById("btnBugSend").addEventListener("click", async () => {
  const titulo = document.getElementById("bugTitle").value.trim();
  const descricao = document.getElementById("bugDesc").value.trim();
  const msg = document.getElementById("bugMsg");
  msg.className = ""; msg.textContent = "enviando…";
  const res = await fetch("/api/bug", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ titulo, descricao }),
  });
  const data = await res.json().catch(() => ({}));
  msg.innerHTML = "";
  if (res.ok && data.ok) {
    document.getElementById("bugTitle").value = "";
    document.getElementById("bugDesc").value = "";
    msg.className = "ok-text";
    msg.appendChild(document.createTextNode("registrado! acompanhe em "));
    const link = el("a", { href: data.url, target: "_blank" }, data.url);
    msg.appendChild(link);
  } else {
    msg.className = "err-text";
    msg.textContent = data.error || "não foi possível registrar";
  }
});

document.getElementById("btnLogout").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  location.reload();
});
document.getElementById("btnTeamAdd").addEventListener("click", addTeamMember);
document.getElementById("btnSync").addEventListener("click", () => runSync(false));
document.getElementById("btnDry").addEventListener("click", () => runSync(true));
document.getElementById("btnCloseTasks").addEventListener("click", async () => {
  const res = await fetch("/api/close-tasks", { method: "POST" });
  const data = await res.json().catch(() => ({}));
  show("msg", res.ok && data.ok
    ? "concluindo tarefas finalizadas no Jira…"
    : (data.error || "não foi possível iniciar"), res.ok ? "" : "warn-text");
});
document.getElementById("btnSaveCfg").addEventListener("click", saveConfig);
document.getElementById("btnTest").addEventListener("click", testConnection);
document.getElementById("btnSaveMapping").addEventListener("click", saveMapping);
document.getElementById("implogFilter").addEventListener("input", renderImportLog);

showPage(location.hash.replace("#", "") || "sync");
loadConfig();
loadImportLog();
loadAudit();
loadSchedule();
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
    parser.add_argument("--users-file", default=auth.DEFAULT_USERS_FILE)
    parser.add_argument("--schedule-file", default=DEFAULT_SCHEDULE_FILE)
    parser.add_argument(
        "--add-user",
        metavar="EMAIL",
        help="cadastra (ou redefine a senha de) um administrador pelo terminal e sai",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="não abrir o navegador automaticamente"
    )
    args = parser.parse_args(argv)

    if args.add_user:
        import getpass

        senha = getpass.getpass(f"senha para {args.add_user}: ")
        try:
            auth.add_user(Path(args.users_file), args.add_user, senha, admin=True)
        except ValueError as exc:
            print(f"erro: {exc}")
            return 2
        print("usuário administrador salvo.")
        return 0

    configure_logging(log_file=args.log_file or None)
    load_env_file(args.env_file)
    runner = SyncRunner(
        Path(args.mapping_file),
        Path(args.state_file),
        Path(args.history_file),
        Path(args.import_log_file),
        Path(args.env_file),
    )
    runner.schedule_path = Path(args.schedule_file)
    Scheduler(runner, runner.schedule_path).start()
    server = App((args.host, args.port), runner, Path(args.users_file))
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
