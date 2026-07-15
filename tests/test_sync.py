from __future__ import annotations

import unittest
from datetime import datetime, timezone

from sync_jira_odoo.config import Config
from sync_jira_odoo.jira_client import adf_to_text
from sync_jira_odoo.odoo_client import OdooError
from sync_jira_odoo.sync import SyncEngine

from .fakes import FakeJira, FakeOdoo, make_issue, make_worklog

SINCE = datetime(2026, 6, 1, tzinfo=timezone.utc)


def make_config(**overrides) -> Config:
    values = dict(
        odoo_url="https://odoo.test",
        odoo_db="db",
        odoo_user="u",
        odoo_api_key="k",
        jira_url="https://jira.test",
        jira_user="u",
        jira_api_token="t",
    )
    values.update(overrides)
    return Config(**values)


def make_odoo_with_employee() -> FakeOdoo:
    return FakeOdoo(
        {
            "hr.employee": [
                {
                    "id": 7,
                    "name": "Diego Gozer",
                    "work_email": "diego@dexterityit.com.br",
                    "user_id": False,
                }
            ]
        }
    )


class AdfToTextTest(unittest.TestCase):
    def test_extracts_nested_text(self):
        doc = make_worklog(comment_text="Reunião com cliente")["comment"]
        self.assertEqual(adf_to_text(doc), "Reunião com cliente")

    def test_handles_none_and_empty(self):
        self.assertEqual(adf_to_text(None), "")
        self.assertEqual(adf_to_text({"type": "doc", "content": []}), "")


class SyncEngineTest(unittest.TestCase):
    def setUp(self):
        self.odoo = make_odoo_with_employee()
        self.jira = FakeJira([make_worklog()], {"35772": make_issue()})
        self.engine = SyncEngine(self.jira, self.odoo, make_config())

    def line_names(self):
        return [r["name"] for r in self.odoo.data.get("account.analytic.line", [])]

    def test_first_run_creates_project_task_and_line(self):
        result = self.engine.run(SINCE)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.warnings, [])

        projects = self.odoo.data["project.project"]
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["name"], "[CDV] Greenfield | Casa dos Ventos")
        self.assertTrue(projects[0]["allow_timesheets"])

        tasks = self.odoo.data["project.task"]
        self.assertEqual(tasks[0]["name"], "[CDV-331] Reunião de preparação")

        lines = self.odoo.data["account.analytic.line"]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["name"], "Feito [jira-worklog:27279]")
        self.assertEqual(line["date"], "2026-06-08")  # data local do fuso -03:00
        self.assertEqual(line["unit_amount"], 0.33)  # 1200s
        self.assertEqual(line["employee_id"], 7)
        self.assertEqual(line["project_id"], projects[0]["id"])
        self.assertEqual(line["task_id"], tasks[0]["id"])

    def test_second_run_is_idempotent(self):
        self.engine.run(SINCE)
        result = SyncEngine(self.jira, self.odoo, make_config()).run(SINCE)
        self.assertEqual((result.created, result.updated, result.skipped), (0, 0, 1))
        self.assertEqual(len(self.line_names()), 1)
        self.assertEqual(len(self.odoo.data["project.project"]), 1)
        self.assertEqual(len(self.odoo.data["project.task"]), 1)

    def test_changed_worklog_updates_line(self):
        self.engine.run(SINCE)
        self.jira.worklogs["27279"]["timeSpentSeconds"] = 3600
        result = SyncEngine(self.jira, self.odoo, make_config()).run(SINCE)
        self.assertEqual((result.created, result.updated), (0, 1))
        self.assertEqual(self.odoo.data["account.analytic.line"][0]["unit_amount"], 1.0)

    def test_dry_run_writes_nothing(self):
        result = self.engine.run(SINCE, dry_run=True)
        self.assertEqual(result.created, 1)
        self.assertNotIn("account.analytic.line", self.odoo.data)
        self.assertNotIn("project.project", self.odoo.data)

    def test_project_filter_skips_other_projects(self):
        engine = SyncEngine(self.jira, self.odoo, make_config(jira_project_keys=["RDF"]))
        result = engine.run(SINCE)
        self.assertEqual((result.created, result.skipped), (0, 1))

    def test_project_map_uses_existing_project(self):
        self.odoo.data["project.project"] = [{"id": 55, "name": "Casa dos Ventos"}]
        engine = SyncEngine(
            self.jira, self.odoo, make_config(project_map={"CDV": "Casa dos Ventos"})
        )
        result = engine.run(SINCE)
        self.assertEqual(result.created, 1)
        self.assertEqual(len(self.odoo.data["project.project"]), 1)
        self.assertEqual(self.odoo.data["account.analytic.line"][0]["project_id"], 55)

    def test_project_map_missing_project_warns_and_skips(self):
        engine = SyncEngine(
            self.jira, self.odoo, make_config(project_map={"CDV": "Não Existe"})
        )
        result = engine.run(SINCE)
        self.assertEqual((result.created, result.skipped), (0, 1))
        self.assertEqual(len(result.warnings), 1)
        self.assertNotIn("account.analytic.line", self.odoo.data)

    def test_author_without_email_uses_account_id_map(self):
        self.jira.worklogs["27279"] = make_worklog(author_email=None)
        engine = SyncEngine(
            self.jira,
            self.odoo,
            make_config(employee_map={"712020:3a98a142": "diego@dexterityit.com.br"}),
        )
        result = engine.run(SINCE)
        self.assertEqual(result.created, 1)
        self.assertEqual(self.odoo.data["account.analytic.line"][0]["employee_id"], 7)

    def test_unresolved_author_skips_with_warning(self):
        self.jira.worklogs["27279"] = make_worklog(author_email="ninguem@exemplo.com")
        result = self.engine.run(SINCE)
        self.assertEqual((result.created, result.skipped), (0, 1))
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("ninguem@exemplo.com", result.warnings[0])

    def test_archived_employee_warns_and_skips_without_aborting(self):
        # o Odoo proíbe timesheet de funcionário arquivado: avisa (com o nome)
        # e pula, em vez de deixar o servidor abortar a carga inteira
        self.odoo.data["hr.employee"] = [
            {
                "id": 12,
                "name": "Ana Luiza de Souza",
                "work_email": "ana@dexterityit.com.br",
                "user_id": False,
                "active": False,
            }
        ]
        self.jira.worklogs["27279"] = make_worklog(author_email="ana@dexterityit.com.br")
        result = self.engine.run(SINCE)
        self.assertEqual((result.created, result.skipped), (0, 1))
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("Ana Luiza de Souza", result.warnings[0])
        self.assertIn("arquivado", result.warnings[0])
        self.assertNotIn("account.analytic.line", self.odoo.data)

    def test_odoo_error_in_one_worklog_does_not_abort_run(self):
        # simula a recusa do servidor (ex.: "funcionário ativo exigido") em um
        # worklog: o run continua e o problema vira aviso + pulado
        original_create = self.odoo.create

        def flaky_create(model, vals):
            if model == "account.analytic.line":
                raise OdooError("Planilhas devem ser criadas com um funcionário ativo")
            return original_create(model, vals)

        self.odoo.create = flaky_create
        result = self.engine.run(SINCE)
        self.assertEqual((result.created, result.skipped), (0, 1))
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("funcionário ativo", result.warnings[0])

    def test_active_employee_preferred_over_archived_with_same_email(self):
        self.odoo.data["hr.employee"] = [
            {"id": 12, "name": "Antiga", "work_email": "diego@dexterityit.com.br",
             "user_id": False, "active": False},
            {"id": 13, "name": "Atual", "work_email": "diego@dexterityit.com.br",
             "user_id": False, "active": True},
        ]
        result = self.engine.run(SINCE)
        self.assertEqual(result.created, 1)
        self.assertEqual(self.odoo.data["account.analytic.line"][0]["employee_id"], 13)

    def test_project_map_finds_archived_project(self):
        self.odoo.data["project.project"] = [
            {"id": 55, "name": "Casa dos Ventos", "active": False}
        ]
        engine = SyncEngine(
            self.jira, self.odoo, make_config(project_map={"CDV": "Casa dos Ventos"})
        )
        result = engine.run(SINCE)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.warnings, [])
        self.assertEqual(self.odoo.data["account.analytic.line"][0]["project_id"], 55)
        # continua arquivado: histórico preservado sem reativar o projeto
        self.assertFalse(self.odoo.data["project.project"][0]["active"])

    def test_employee_found_via_user_login(self):
        self.odoo.data["hr.employee"] = [
            {"id": 9, "name": "Julian", "work_email": False, "user_id": 31}
        ]
        self.odoo.data["res.users"] = [{"id": 31, "login": "julian@dexterityit.com.br"}]
        self.jira.worklogs["27279"] = make_worklog(author_email="julian@dexterityit.com.br")
        result = self.engine.run(SINCE)
        self.assertEqual(result.created, 1)
        self.assertEqual(self.odoo.data["account.analytic.line"][0]["employee_id"], 9)

    def test_worklog_without_comment_uses_issue_summary(self):
        self.jira.worklogs["27279"] = make_worklog(comment_text=None)
        self.engine.run(SINCE)
        self.assertEqual(
            self.line_names(), ["Reunião de preparação [jira-worklog:27279]"]
        )

    def test_deleted_worklog_removed_only_with_delete_flag(self):
        self.engine.run(SINCE)
        self.jira.worklogs = {}
        self.jira.deleted = [27279]

        result = SyncEngine(self.jira, self.odoo, make_config()).run(SINCE)
        self.assertEqual(result.deleted, 0)
        self.assertEqual(len(self.line_names()), 1)

        result = SyncEngine(self.jira, self.odoo, make_config()).run(SINCE, delete=True)
        self.assertEqual(result.deleted, 1)
        self.assertEqual(self.line_names(), [])


if __name__ == "__main__":
    unittest.main()
