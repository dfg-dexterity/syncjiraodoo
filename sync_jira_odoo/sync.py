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
from .odoo_client import OdooClient, OdooError

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


def _field_text(value) -> str:
    """Extrai o texto de um campo do Jira: select ({'value': ...}), texto puro
    ou lista (multi-select — usa o primeiro valor)."""
    if isinstance(value, dict):
        return str(value.get("value") or value.get("name") or "").strip()
    if isinstance(value, list):
        return _field_text(value[0]) if value else ""
    if value is None:
        return ""
    return str(value).strip()


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    warnings: list[str] = field(default_factory=list)
    # um registro por timesheet criado/atualizado/removido, para auditoria
    items: list[dict] = field(default_factory=list)

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
        self._routing_projects = {k.upper() for k in config.department_projects}
        self._dept_field_id: str | None = None
        self._issue_fields: tuple[str, ...] = ("summary", "project")
        self._warned_departments: set[str] = set()

    # ------------------------------------------------------------------ run

    def run(
        self,
        since: datetime,
        dry_run: bool = False,
        delete: bool = False,
    ) -> SyncResult:
        result = SyncResult()
        since_ms = int(since.timestamp() * 1000)

        if self._routing_projects:
            self._dept_field_id = self.jira.find_field_id(self.cfg.department_field)
            if self._dept_field_id:
                self._issue_fields = ("summary", "project", self._dept_field_id)
            else:
                result.warn(
                    f"campo '{self.cfg.department_field}' não encontrado no Jira; "
                    f"worklogs de {', '.join(sorted(self._routing_projects))} serão pulados"
                )

        ids = self.jira.updated_worklog_ids(since_ms)
        log.info("worklogs alterados desde %s: %d", since.isoformat(), len(ids))
        consecutive_errors = 0
        for worklog in self.jira.get_worklogs(ids):
            try:
                self._sync_worklog(worklog, result, dry_run)
                consecutive_errors = 0
            except OdooError as exc:
                # um worklog problemático não pode derrubar a carga inteira;
                # só aborta se o Odoo falhar repetidamente (problema sistêmico)
                consecutive_errors += 1
                result.warn(f"worklog {worklog.get('id')} pulado por erro do Odoo: {exc}")
                result.skipped += 1
                if consecutive_errors >= 20:
                    raise

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
        issue = self.jira.get_issue(worklog["issueId"], self._issue_fields)
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

        if project_key in self._routing_projects:
            project_id = self._resolve_department_project(issue, issue_key, result)
        else:
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

        autor = worklog["author"].get("displayName", "")
        if not existing:
            log.info("criando timesheet: %s %s (%sh)", issue_key, vals["date"], vals["unit_amount"])
            if not dry_run:
                self.odoo.create("account.analytic.line", vals)
            result.created += 1
            result.items.append({
                "acao": "criado",
                "issue": issue_key,
                "data": vals["date"],
                "horas": vals["unit_amount"],
                "autor": autor,
                "descricao": description[:120],
            })
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
            result.items.append({
                "acao": "atualizado",
                "issue": issue_key,
                "data": vals["date"],
                "horas": vals["unit_amount"],
                "autor": autor,
                "descricao": description[:120],
                "campos": sorted(changed),
            })
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
            for line in lines:
                result.items.append({
                    "acao": "removido",
                    "worklog": w_id,
                    "descricao": str(line.get("name", ""))[:120],
                })

    # ------------------------------------------------------ projeto / tarefa

    def _search_active_first(self, model: str, domain: list, fields: list[str]) -> list[dict]:
        """Busca registros ativos e, se nada for encontrado, inclui os
        arquivados — o histórico de projetos/funcionários arquivados no Odoo
        continua válido para receber timesheets."""
        rows = self.odoo.search_read(model, domain, fields, limit=1)
        if not rows:
            rows = self.odoo.search_read(model, domain, fields, limit=1, include_archived=True)
        return rows

    def _ensure_project(
        self, key: str, jira_name: str, result: SyncResult, dry_run: bool
    ) -> int | None:
        if key in self._project_cache:
            return self._project_cache[key]

        mapped_name = self.cfg.project_map.get(key)
        if mapped_name:
            rows = self._search_active_first(
                "project.project", [("name", "=", mapped_name)], ["name"]
            )
            project_id = rows[0]["id"] if rows else None
            if project_id is None:
                result.warn(
                    f"projeto Jira {key} mapeado para '{mapped_name}', mas esse "
                    "projeto não existe no Odoo (nem arquivado); worklogs serão pulados"
                )
        else:
            marker = f"[{key}]"
            rows = self._search_active_first(
                "project.project", [("name", "like", marker)], ["name"]
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

    def _resolve_department_project(
        self, issue: dict, issue_key: str, result: SyncResult
    ) -> int | None:
        """Nos projetos com roteamento por departamento, o projeto Odoo vem do
        valor do campo da issue (ex.: 'Departamento Dexterity'), nunca da key.
        Nada é criado automaticamente: o projeto Odoo precisa existir."""
        if self._dept_field_id is None:
            return None  # aviso único já emitido no início da execução

        dept = _field_text(issue["fields"].get(self._dept_field_id))
        if not dept:
            result.warn(
                f"{issue_key}: campo '{self.cfg.department_field}' vazio no Jira; "
                "preencha o departamento na issue — worklog pulado"
            )
            return None

        odoo_name = self.cfg.department_map.get(dept.lower())
        if not odoo_name:
            if dept.lower() not in self._warned_departments:
                self._warned_departments.add(dept.lower())
                result.warn(
                    f"departamento '{dept}' sem projeto Odoo no de-para "
                    "(seção roteamento por departamento) — worklogs pulados"
                )
            return None

        cache_key = f"dept::{odoo_name}"
        if cache_key in self._project_cache:
            return self._project_cache[cache_key]
        rows = self._search_active_first("project.project", [("name", "=", odoo_name)], ["name"])
        project_id = rows[0]["id"] if rows else None
        if project_id is None:
            result.warn(
                f"departamento '{dept}' aponta para o projeto '{odoo_name}', mas ele "
                "não existe no Odoo (nem arquivado); worklogs serão pulados"
            )
        self._project_cache[cache_key] = project_id
        return project_id

    def _ensure_task(self, project_id: int, issue_key: str, summary: str, dry_run: bool) -> int:
        if issue_key in self._task_cache:
            return self._task_cache[issue_key]

        marker = f"[{issue_key}]"
        rows = self._search_active_first(
            "project.task",
            [("project_id", "=", project_id), ("name", "like", marker)],
            ["name"],
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

        employee = self._find_employee(email) if email else None
        if employee is None and self.cfg.default_employee_email:
            employee = self._find_employee(self.cfg.default_employee_email)

        if employee is None:
            employee_id = None
            result.warn(
                f"autor Jira sem funcionário correspondente no Odoo: "
                f"{author.get('displayName', '?')} (accountId={account_id}, "
                f"email={jira_email or 'oculto'}); mapeie em JIRA_ODOO_EMPLOYEE_MAP"
            )
        elif not employee.get("active", True):
            # o Odoo proíbe criar timesheet para funcionário arquivado
            employee_id = None
            result.warn(
                f"funcionário '{employee['name']}' ({email}) está arquivado no Odoo "
                "e o Odoo não permite criar timesheets para arquivados; para importar "
                "o histórico, reative-o temporariamente, rode o sync e arquive de novo "
                "— worklogs pulados por enquanto"
            )
        else:
            employee_id = employee["id"]

        self._employee_cache[account_id] = employee_id
        return employee_id

    def _find_employee(self, email: str) -> dict | None:
        """Prefere funcionários ativos; um arquivado ainda é devolvido (com
        active=False) para o chamador avisar com nome e sobrenome."""
        rows = self._search_active_first(
            "hr.employee", [("work_email", "=ilike", email)], ["name", "active"]
        )
        if rows:
            return rows[0]
        users = self._search_active_first("res.users", [("login", "=ilike", email)], ["name"])
        if users:
            rows = self._search_active_first(
                "hr.employee", [("user_id", "=", users[0]["id"])], ["name", "active"]
            )
            if rows:
                return rows[0]
        return None
