"""Motor de sincronização: worklogs do Jira → account.analytic.line no Odoo.

Estratégia de idempotência sem estado no servidor: cada registro criado no
Odoo carrega um marcador textual no nome —

  projeto   "[RDF] Interno | Rotinas e Procedimentos ADM"
  tarefa    "[RDF-123] Resumo da issue"
  timesheet "Comentário do worklog [jira-worklog:27279]"

— que permite localizar o registro correspondente em execuções futuras e
criar/atualizar/remover sem duplicar.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from .config import Config
from .jira_client import JiraClient, adf_to_text
from .odoo_client import OdooClient

log = logging.getLogger("sync_jira_odoo")

WORKLOG_MARKER_RE = re.compile(r"\[jira-worklog:(\d+)\]")


def worklog_marker(worklog_id: str | int) -> str:
    return f"[jira-worklog:{worklog_id}]"


def parse_started(started: str) -> datetime:
    """Converte '2026-06-08T17:00:00.000-0300' em datetime com fuso."""
    return datetime.strptime(started, "%Y-%m-%dT%H:%M:%S.%f%z")


def connection_report(config: Config) -> str:
    """Valida as credenciais do Odoo e do Jira e devolve um resumo legível."""
    odoo = OdooClient(config.odoo_url, config.odoo_db, config.odoo_user, config.odoo_api_key)
    version = odoo.version()
    lines = [
        f"Odoo: {version.get('server_version')} (série {version.get('server_serie')})",
        f"Odoo authenticate(): OK, uid={odoo.uid}",
    ]
    jira = JiraClient(config.jira_url, config.jira_user, config.jira_api_token)
    me = jira.myself()
    lines.append(f"Jira: autenticado como {me.get('displayName')} ({me.get('emailAddress')})")
    return "\n".join(lines)


def _m2o_id(value) -> int | None:
    """Normaliza um many2one do Odoo (False, int ou [id, label]) para id."""
    if isinstance(value, (list, tuple)):
        return value[0]
    return value or None


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        log.warning(message)


class SyncEngine:
    def __init__(self, jira: JiraClient, odoo: OdooClient, config: Config):
        self.jira = jira
        self.odoo = odoo
        self.cfg = config
        self._project_cache: dict[str, int | None] = {}
        self._task_cache: dict[str, int] = {}
        self._employee_cache: dict[str, int | None] = {}

    # ------------------------------------------------------------------ run

    def run(
        self,
        since: datetime,
        dry_run: bool = False,
        delete: bool = False,
    ) -> SyncResult:
        result = SyncResult()
        since_ms = int(since.timestamp() * 1000)

        ids = self.jira.updated_worklog_ids(since_ms)
        log.info("worklogs alterados desde %s: %d", since.isoformat(), len(ids))
        for worklog in self.jira.get_worklogs(ids):
            self._sync_worklog(worklog, result, dry_run)

        deleted_ids = self.jira.deleted_worklog_ids(since_ms)
        if deleted_ids:
            if delete:
                self._delete_worklogs(deleted_ids, result, dry_run)
            else:
                log.info(
                    "%d worklogs removidos no Jira; use --delete para remover "
                    "os timesheets correspondentes no Odoo",
                    len(deleted_ids),
                )
        return result

    # ------------------------------------------------------------- worklogs

    def _sync_worklog(self, worklog: dict, result: SyncResult, dry_run: bool) -> None:
        w_id = worklog["id"]
        issue = self.jira.get_issue(worklog["issueId"])
        issue_key = issue["key"]
        project = issue["fields"]["project"]
        project_key = project["key"].upper()

        if self.cfg.jira_project_keys and project_key not in self.cfg.jira_project_keys:
            result.skipped += 1
            return

        employee_id = self._resolve_employee(worklog["author"], result)
        if employee_id is None:
            result.skipped += 1
            return

        project_id = self._ensure_project(project_key, project["name"], result, dry_run)
        if project_id is None:
            result.skipped += 1
            return
        task_id = self._ensure_task(
            project_id, issue_key, issue["fields"].get("summary", ""), dry_run
        )

        started = parse_started(worklog["started"])
        comment = adf_to_text(worklog.get("comment")).strip()
        description = comment or issue["fields"].get("summary", "") or issue_key
        vals = {
            "project_id": project_id,
            "task_id": task_id,
            "employee_id": employee_id,
            "date": started.date().isoformat(),
            "unit_amount": round(worklog["timeSpentSeconds"] / 3600, 2),
            "name": f"{description} {worklog_marker(w_id)}",
        }

        existing = self.odoo.search_read(
            "account.analytic.line",
            [("name", "like", worklog_marker(w_id))],
            ["name", "date", "unit_amount", "project_id", "task_id", "employee_id"],
            limit=2,
        )
        if len(existing) > 1:
            result.warn(
                f"worklog {w_id} ({issue_key}): {len(existing)}+ timesheets com o "
                "mesmo marcador no Odoo; pulando para não corromper dados"
            )
            result.skipped += 1
            return

        if not existing:
            log.info("criando timesheet: %s %s (%sh)", issue_key, vals["date"], vals["unit_amount"])
            if not dry_run:
                self.odoo.create("account.analytic.line", vals)
            result.created += 1
            return

        line = existing[0]
        changed = {
            key: value
            for key, value in vals.items()
            if value != (_m2o_id(line[key]) if key.endswith("_id") else line[key])
        }
        if changed:
            log.info("atualizando timesheet do worklog %s (%s): %s", w_id, issue_key, sorted(changed))
            if not dry_run:
                self.odoo.write("account.analytic.line", [line["id"]], changed)
            result.updated += 1
        else:
            result.skipped += 1

    def _delete_worklogs(self, worklog_ids: list[int], result: SyncResult, dry_run: bool) -> None:
        for w_id in worklog_ids:
            lines = self.odoo.search_read(
                "account.analytic.line",
                [("name", "like", worklog_marker(w_id))],
                ["name"],
            )
            if not lines:
                continue
            ids = [line["id"] for line in lines]
            log.info("removendo %d timesheet(s) do worklog %s", len(ids), w_id)
            if not dry_run:
                self.odoo.unlink("account.analytic.line", ids)
            result.deleted += len(ids)

    # ------------------------------------------------------ projeto / tarefa

    def _ensure_project(
        self, key: str, jira_name: str, result: SyncResult, dry_run: bool
    ) -> int | None:
        if key in self._project_cache:
            return self._project_cache[key]

        mapped_name = self.cfg.project_map.get(key)
        if mapped_name:
            rows = self.odoo.search_read(
                "project.project", [("name", "=", mapped_name)], ["name"], limit=1
            )
            project_id = rows[0]["id"] if rows else None
            if project_id is None:
                result.warn(
                    f"projeto Jira {key} mapeado para '{mapped_name}', mas esse "
                    "projeto não existe no Odoo; worklogs serão pulados"
                )
        else:
            marker = f"[{key}]"
            rows = self.odoo.search_read(
                "project.project", [("name", "like", marker)], ["name"], limit=1
            )
            if rows:
                project_id = rows[0]["id"]
            else:
                name = f"{marker} {jira_name}"
                log.info("criando projeto no Odoo: %s", name)
                if dry_run:
                    project_id = -1  # placeholder para o restante do dry-run
                else:
                    project_id = self.odoo.create(
                        "project.project", {"name": name, "allow_timesheets": True}
                    )

        self._project_cache[key] = project_id
        return project_id

    def _ensure_task(self, project_id: int, issue_key: str, summary: str, dry_run: bool) -> int:
        if issue_key in self._task_cache:
            return self._task_cache[issue_key]

        marker = f"[{issue_key}]"
        rows = self.odoo.search_read(
            "project.task",
            [("project_id", "=", project_id), ("name", "like", marker)],
            ["name"],
            limit=1,
        )
        if rows:
            task_id = rows[0]["id"]
        else:
            name = f"{marker} {summary}".strip()
            log.info("criando tarefa no Odoo: %s", name)
            if dry_run:
                task_id = -1
            else:
                task_id = self.odoo.create(
                    "project.task", {"name": name, "project_id": project_id}
                )
        self._task_cache[issue_key] = task_id
        return task_id

    # ------------------------------------------------------------ funcionário

    def _resolve_employee(self, author: dict, result: SyncResult) -> int | None:
        account_id = author.get("accountId", "")
        if account_id in self._employee_cache:
            return self._employee_cache[account_id]

        jira_email = author.get("emailAddress", "")
        email = (
            self.cfg.employee_map.get(account_id)
            or (jira_email and self.cfg.employee_map.get(jira_email))
            or jira_email
        )

        employee_id = self._find_employee(email) if email else None
        if employee_id is None and self.cfg.default_employee_email:
            employee_id = self._find_employee(self.cfg.default_employee_email)
        if employee_id is None:
            result.warn(
                f"autor Jira sem funcionário correspondente no Odoo: "
                f"{author.get('displayName', '?')} (accountId={account_id}, "
                f"email={jira_email or 'oculto'}); mapeie em JIRA_ODOO_EMPLOYEE_MAP"
            )

        self._employee_cache[account_id] = employee_id
        return employee_id

    def _find_employee(self, email: str) -> int | None:
        rows = self.odoo.search_read(
            "hr.employee", [("work_email", "=ilike", email)], ["name"], limit=1
        )
        if rows:
            return rows[0]["id"]
        users = self.odoo.search_read(
            "res.users", [("login", "=ilike", email)], ["name"], limit=1
        )
        if users:
            rows = self.odoo.search_read(
                "hr.employee", [("user_id", "=", users[0]["id"])], ["name"], limit=1
            )
            if rows:
                return rows[0]["id"]
        return None
