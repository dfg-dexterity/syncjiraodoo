"""Fechamento de período: Clockwork Pro (worklogs do Jira) × Odoo.

Para um dia, uma semana ou um mês, soma as horas apontadas nos dois lados e
compara projeto a projeto e pessoa a pessoa, apontando cada discrepância:

  ok          worklog gravado no Odoo com as mesmas horas, data e projeto
  divergente  gravado, mas horas/data/projeto diferem
  faltando    existe no Clockwork, não existe no Odoo
  duplicado   dois ou mais timesheets no Odoo para o mesmo worklog
  so_odoo     timesheet com marcador de worklog que não existe no Clockwork
              dentro do período (apagado ou com a data alterada no Jira)
  manual      timesheet lançado à mão no Odoo, sem origem no Clockwork
  sem_destino worklog cujo projeto Odoo não pôde ser determinado (ex.:
              Departamento Dexterity vazio) — nunca entra no Odoo até corrigir

Somente leitura: nada é alterado em nenhum dos dois sistemas.
"""

from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import date, timedelta

from .sync import (
    WORKLOG_MARKER_RE,
    SyncEngine,
    SyncResult,
    _field_text,
    _m2o_id,
    parse_started,
)

log = logging.getLogger("sync_jira_odoo")

PERIOD_KINDS = ("dia", "semana", "mes")
HOURS_TOLERANCE = 0.005  # unit_amount do Odoo tem 2 casas


def period_bounds(kind: str, ref: date) -> tuple[date, date]:
    """Limites (inclusive) do período que contém a data de referência."""
    if kind == "dia":
        return ref, ref
    if kind == "semana":
        start = ref - timedelta(days=ref.weekday())  # segunda-feira
        return start, start + timedelta(days=6)
    if kind == "mes":
        start = ref.replace(day=1)
        last_day = calendar.monthrange(ref.year, ref.month)[1]
        return start, ref.replace(day=last_day)
    raise ValueError(f"tipo de período inválido: {kind}")


def period_label(kind: str, start: date, end: date) -> str:
    if kind == "dia":
        return f"dia {start.strftime('%d/%m/%Y')}"
    if kind == "semana":
        return f"semana de {start.strftime('%d/%m')} a {end.strftime('%d/%m/%Y')}"
    return f"mês {start.strftime('%m/%Y')}"


def _hours(seconds: int | float) -> float:
    return round(float(seconds) / 3600, 2)


class PeriodClosing:
    """Executa o fechamento usando os clientes e o de-para do motor de sync."""

    def __init__(self, engine: SyncEngine):
        self.engine = engine
        self.jira = engine.jira
        self.odoo = engine.odoo
        self.cfg = engine.cfg

    # ------------------------------------------------------------ destino

    def expected_project(self, issue: dict) -> dict:
        """Onde o worklog desta issue deve estar no Odoo, segundo o de-para:
        {"name": nome exato} ou {"marker": "[KEY]"} ou {"error": motivo}."""
        project = issue["fields"]["project"]
        key = str(project.get("key", "")).upper()
        expected = {"name": None, "marker": None, "error": None}
        if key in self.engine._routing_projects:
            if self.engine._dept_field_id is None:
                expected["error"] = f"campo '{self.cfg.department_field}' não encontrado no Jira"
                return expected
            dept = _field_text(issue["fields"].get(self.engine._dept_field_id))
            if not dept:
                expected["error"] = f"campo '{self.cfg.department_field}' vazio na issue"
                return expected
            name = self.cfg.department_map.get(dept.lower())
            if not name and self.cfg.department_concat:
                name = f"{str(project.get('name', '')).strip()} | {dept}"
            if not name:
                expected["error"] = f"departamento '{dept}' sem projeto Odoo no de-para"
                return expected
            expected["name"] = name
            return expected
        mapped = self.cfg.project_map.get(key)
        if mapped:
            expected["name"] = mapped
        else:
            expected["marker"] = f"[{key}]"
        return expected

    @staticmethod
    def project_matches(expected: dict, odoo_name: str) -> bool:
        if expected.get("name"):
            return expected["name"].strip().lower() == str(odoo_name).strip().lower()
        if expected.get("marker"):
            return expected["marker"] in str(odoo_name)
        return False

    # -------------------------------------------------------------- coleta

    def _collect_jira(self, start: date, end: date) -> dict[str, dict]:
        keys = [k.upper() for k in self.cfg.jira_project_keys]
        jql = f'worklogDate >= "{start.isoformat()}" AND worklogDate <= "{end.isoformat()}"'
        if keys:
            jql = f"project in ({', '.join(keys)}) AND " + jql
        issues = self.jira.search_issues(jql, self.engine._issue_fields)
        log.info("fechamento: %d issue(s) com apontamentos no período", len(issues))
        items: dict[str, dict] = {}
        for issue in issues:
            project = issue["fields"]["project"]
            if keys and str(project.get("key", "")).upper() not in keys:
                continue
            expected = self.expected_project(issue)
            if expected["name"]:
                destino = expected["name"]
            elif expected["marker"]:
                destino = f"{expected['marker']} {project.get('name', '')}".strip()
            else:
                destino = None
            for worklog in self.jira.issue_worklogs(issue["key"]):
                day = parse_started(worklog["started"]).date()
                if not (start <= day <= end):
                    continue
                items[str(worklog["id"])] = {
                    "worklog": str(worklog["id"]),
                    "issue": issue["key"],
                    "projeto_jira": str(project.get("name", "")),
                    "projeto_esperado": destino,
                    "expected": expected,
                    "autor": worklog.get("author", {}).get("displayName", ""),
                    "data": day.isoformat(),
                    "horas": _hours(worklog["timeSpentSeconds"]),
                }
        return items

    def _names(self, model: str, rows: list[dict], field: str) -> dict[int, str]:
        """Resolve many2one → nome (aceita [id, nome] do XML-RPC ou só o id)."""
        labels: dict[int, str] = {}
        ids: set[int] = set()
        for row in rows:
            value = row.get(field)
            if isinstance(value, (list, tuple)) and len(value) > 1:
                labels[value[0]] = str(value[1])
            elif value:
                ids.add(value)
        missing = [i for i in ids if i not in labels]
        if missing:
            for rec in self.odoo.search_read(
                model, [("id", "in", missing)], ["name"], include_archived=True
            ):
                labels[rec["id"]] = str(rec["name"])
        return labels

    def _collect_odoo(self, start: date, end: date) -> tuple[dict[str, list[dict]], list[dict]]:
        rows = self.odoo.search_read(
            "account.analytic.line",
            [
                ("date", ">=", start.isoformat()),
                ("date", "<=", end.isoformat()),
                ("project_id", "!=", False),
            ],
            ["name", "date", "unit_amount", "project_id", "employee_id"],
        )
        log.info("fechamento: %d timesheet(s) no Odoo no período", len(rows))
        projects = self._names("project.project", rows, "project_id")
        employees = self._names("hr.employee", rows, "employee_id")
        by_worklog: dict[str, list[dict]] = defaultdict(list)
        manual: list[dict] = []
        for row in rows:
            name = str(row.get("name", ""))
            entry = {
                "id": row["id"],
                "data": str(row.get("date", "")),
                "horas": round(float(row.get("unit_amount") or 0), 2),
                "projeto": projects.get(_m2o_id(row.get("project_id")), "?"),
                "pessoa": employees.get(_m2o_id(row.get("employee_id")), "?"),
                "descricao": name[:120],
            }
            match = WORKLOG_MARKER_RE.search(name)
            if match:
                by_worklog[match.group(1)].append(entry)
            else:
                manual.append(entry)
        return by_worklog, manual

    # ----------------------------------------------------------------- run

    def run(self, kind: str, ref: date) -> dict:
        start, end = period_bounds(kind, ref)
        label = period_label(kind, start, end)
        log.info("fechamento do %s: comparando Clockwork × Odoo", label)
        result = SyncResult()
        self.engine._prepare_department_field(result)

        jira_items = self._collect_jira(start, end)
        odoo_by_worklog, manual = self._collect_odoo(start, end)

        projects: dict[str, dict] = defaultdict(lambda: {"jira": 0.0, "odoo": 0.0})
        people: dict[str, dict] = defaultdict(lambda: {"jira": 0.0, "odoo": 0.0})
        items: list[dict] = []
        counts: dict[str, int] = defaultdict(int)

        for w_id, jira in jira_items.items():
            lines = odoo_by_worklog.pop(w_id, [])
            destino = jira["projeto_esperado"] or f"(sem destino) {jira['projeto_jira']}"
            projects[destino]["jira"] += jira["horas"]
            people[jira["autor"]]["jira"] += jira["horas"]
            odoo_hours = round(sum(line["horas"] for line in lines), 2) if lines else None
            for line in lines:
                projects[line["projeto"]]["odoo"] += line["horas"]
                people[jira["autor"]]["odoo"] += line["horas"]

            detalhe = ""
            if jira["expected"]["error"]:
                status = "sem_destino"
                detalhe = jira["expected"]["error"]
            elif not lines:
                status = "faltando"
            elif len(lines) > 1:
                status = "duplicado"
                detalhe = f"{len(lines)} timesheets no Odoo"
            else:
                line = lines[0]
                diffs = []
                if abs(line["horas"] - jira["horas"]) > HOURS_TOLERANCE:
                    diffs.append("horas")
                if line["data"] != jira["data"]:
                    diffs.append("data")
                if not self.project_matches(jira["expected"], line["projeto"]):
                    diffs.append("projeto")
                status = "divergente" if diffs else "ok"
                detalhe = ", ".join(diffs)
            counts[status] += 1
            if status != "ok":
                items.append({
                    "status": status,
                    "detalhe": detalhe,
                    "worklog": w_id,
                    "issue": jira["issue"],
                    "pessoa": jira["autor"],
                    "data": jira["data"],
                    "horas_jira": jira["horas"],
                    "horas_odoo": odoo_hours,
                    "projeto_esperado": destino,
                    "projeto_odoo": lines[0]["projeto"] if lines else None,
                })

        for w_id, lines in odoo_by_worklog.items():
            for line in lines:
                projects[line["projeto"]]["odoo"] += line["horas"]
                people[line["pessoa"]]["odoo"] += line["horas"]
                counts["so_odoo"] += 1
                items.append({
                    "status": "so_odoo",
                    "detalhe": "worklog apagado ou movido de data no Jira",
                    "worklog": w_id,
                    "issue": None,
                    "pessoa": line["pessoa"],
                    "data": line["data"],
                    "horas_jira": None,
                    "horas_odoo": line["horas"],
                    "projeto_esperado": None,
                    "projeto_odoo": line["projeto"],
                    "descricao": line["descricao"],
                })

        for line in manual:
            projects[line["projeto"]]["odoo"] += line["horas"]
            people[line["pessoa"]]["odoo"] += line["horas"]
            counts["manual"] += 1
            items.append({
                "status": "manual",
                "detalhe": "lançado à mão no Odoo, sem origem no Clockwork",
                "worklog": None,
                "issue": None,
                "pessoa": line["pessoa"],
                "data": line["data"],
                "horas_jira": None,
                "horas_odoo": line["horas"],
                "projeto_esperado": None,
                "projeto_odoo": line["projeto"],
                "descricao": line["descricao"],
            })

        def rows(table: dict[str, dict], key: str) -> list[dict]:
            out = []
            for name, hours in sorted(table.items(), key=lambda kv: kv[0].lower()):
                jira_h = round(hours["jira"], 2)
                odoo_h = round(hours["odoo"], 2)
                out.append({
                    key: name,
                    "jira": jira_h,
                    "odoo": odoo_h,
                    "diff": round(odoo_h - jira_h, 2),
                })
            return out

        jira_total = round(sum(i["horas"] for i in jira_items.values()), 2)
        odoo_total = round(sum(p["odoo"] for p in projects.values()), 2)
        problems = sum(n for status, n in counts.items() if status != "ok")
        status_order = ["faltando", "divergente", "sem_destino", "duplicado", "so_odoo", "manual"]
        items.sort(key=lambda i: (status_order.index(i["status"]), i["data"], i["pessoa"]))

        record = {
            "kind": kind,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": label,
            "jira_hours": jira_total,
            "odoo_hours": odoo_total,
            "diff": round(odoo_total - jira_total, 2),
            "ok": problems == 0,
            "n_worklogs": len(jira_items),
            "counts": dict(counts),
            "projects": rows(projects, "projeto"),
            "people": rows(people, "pessoa"),
            "items": items,
            "warnings": result.warnings,
            "errors": result.errors,
        }
        log.info(
            "fechamento do %s: Clockwork %.2fh × Odoo %.2fh (%d worklogs, %d problema(s))",
            label, jira_total, odoo_total, len(jira_items), problems,
        )
        return record
