from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from sync_jira_odoo import storage
from sync_jira_odoo.closing import PeriodClosing, period_bounds, period_label
from sync_jira_odoo.sync import SyncEngine

from .fakes import FakeJira, FakeOdoo, make_issue, make_worklog
from .test_sync import make_config


class PeriodBoundsTest(unittest.TestCase):
    def test_day_week_month(self):
        ref = date(2026, 11, 5)  # quinta-feira
        self.assertEqual(period_bounds("dia", ref), (ref, ref))
        self.assertEqual(period_bounds("semana", ref), (date(2026, 11, 2), date(2026, 11, 8)))
        self.assertEqual(period_bounds("mes", ref), (date(2026, 11, 1), date(2026, 11, 30)))
        self.assertEqual(period_bounds("mes", date(2026, 2, 10)), (date(2026, 2, 1), date(2026, 2, 28)))
        with self.assertRaises(ValueError):
            period_bounds("ano", ref)

    def test_labels(self):
        self.assertEqual(period_label("dia", date(2026, 11, 5), date(2026, 11, 5)), "dia 05/11/2026")
        self.assertIn("02/11 a 08/11/2026", period_label("semana", date(2026, 11, 2), date(2026, 11, 8)))
        self.assertEqual(period_label("mes", date(2026, 11, 1), date(2026, 11, 30)), "mês 11/2026")


def line(lid, worklog_id, hours, day="2026-06-08", project=50, employee=7, name=None):
    marker = f"[jira-worklog:{worklog_id}]" if worklog_id else ""
    return {
        "id": lid,
        "name": name or f"Feito {marker}".strip(),
        "date": day,
        "unit_amount": hours,
        "project_id": project,
        "employee_id": employee,
    }


class PeriodClosingTest(unittest.TestCase):
    """Semana de 08/06/2026 (segunda) a 14/06/2026."""

    REF = date(2026, 6, 10)

    def build(self, worklogs, lines, issues=None, **cfg):
        issues = issues or {"35772": make_issue()}
        odoo = FakeOdoo({
            "project.project": [
                {"id": 50, "name": "Casa dos Ventos"},
                {"id": 51, "name": "Outro Projeto"},
                {"id": 60, "name": "ITPR | Tarefas Avulsas | FI - Financeiro"},
            ],
            "hr.employee": [{"id": 7, "name": "Diego Gozer"}, {"id": 8, "name": "Ana Luiza"}],
            "account.analytic.line": lines,
        })
        config = make_config(project_map={"CDV": "Casa dos Ventos"}, **cfg)
        jira = FakeJira(worklogs, issues, fields={"Departamento Dexterity": "customfield_10052"})
        return PeriodClosing(SyncEngine(jira, odoo, config))

    def test_period_closes_clean_when_everything_matches(self):
        closing = self.build(
            [make_worklog(wid="1", seconds=3600, started="2026-06-08T09:00:00.000-0300"),
             make_worklog(wid="2", seconds=1800, started="2026-06-09T09:00:00.000-0300")],
            [line(100, "1", 1.0), line(101, "2", 0.5, day="2026-06-09")],
        )
        record = closing.run("semana", self.REF)
        self.assertTrue(record["ok"])
        self.assertEqual((record["jira_hours"], record["odoo_hours"], record["diff"]), (1.5, 1.5, 0.0))
        self.assertEqual(record["counts"], {"ok": 2})
        self.assertEqual(record["items"], [])
        self.assertEqual(record["projects"], [{"projeto": "Casa dos Ventos", "jira": 1.5, "odoo": 1.5, "diff": 0.0}])
        self.assertEqual(record["people"], [{"pessoa": "Diego Gozer", "jira": 1.5, "odoo": 1.5, "diff": 0.0}])
        self.assertEqual(record["label"], "semana de 08/06 a 14/06/2026")

    def test_worklogs_outside_period_are_ignored(self):
        closing = self.build(
            [make_worklog(wid="1", seconds=3600, started="2026-06-08T09:00:00.000-0300"),
             make_worklog(wid="9", seconds=7200, started="2026-06-20T09:00:00.000-0300")],
            [line(100, "1", 1.0), line(109, "9", 2.0, day="2026-06-20")],
        )
        record = closing.run("semana", self.REF)
        self.assertEqual(record["n_worklogs"], 1)
        self.assertEqual(record["jira_hours"], 1.0)
        self.assertEqual(record["odoo_hours"], 1.0)
        self.assertTrue(record["ok"])

    def test_detects_every_kind_of_discrepancy(self):
        closing = self.build(
            [
                make_worklog(wid="1", seconds=3600, started="2026-06-08T09:00:00.000-0300"),  # ok
                make_worklog(wid="2", seconds=3600, started="2026-06-09T09:00:00.000-0300"),  # horas diferem
                make_worklog(wid="3", seconds=1800, started="2026-06-10T09:00:00.000-0300"),  # falta no Odoo
                make_worklog(wid="4", seconds=1800, started="2026-06-11T09:00:00.000-0300"),  # projeto errado
            ],
            [
                line(100, "1", 1.0),
                line(101, "2", 2.0, day="2026-06-09"),
                line(103, "4", 0.5, day="2026-06-11", project=51),
                line(104, "77", 3.0, day="2026-06-12"),            # worklog 77 não existe no Jira
                line(105, None, 1.5, day="2026-06-12", employee=8, name="Reunião lançada à mão"),
            ],
        )
        record = closing.run("semana", self.REF)
        self.assertFalse(record["ok"])
        self.assertEqual(record["jira_hours"], 3.0)
        self.assertEqual(record["odoo_hours"], 8.0)
        self.assertEqual(record["diff"], 5.0)
        self.assertEqual(record["counts"], {
            "ok": 1, "divergente": 2, "faltando": 1, "so_odoo": 1, "manual": 1,
        })
        by_status = {(i["status"], i.get("worklog")): i for i in record["items"]}
        self.assertEqual(by_status[("divergente", "2")]["detalhe"], "horas")
        self.assertEqual(by_status[("divergente", "4")]["detalhe"], "projeto")
        self.assertEqual(by_status[("divergente", "4")]["projeto_odoo"], "Outro Projeto")
        self.assertEqual(by_status[("faltando", "3")]["horas_odoo"], None)
        self.assertEqual(by_status[("so_odoo", "77")]["horas_odoo"], 3.0)
        self.assertEqual(by_status[("manual", None)]["pessoa"], "Ana Luiza")
        projects = {p["projeto"]: p for p in record["projects"]}
        self.assertEqual(projects["Casa dos Ventos"]["jira"], 3.0)
        self.assertEqual(projects["Casa dos Ventos"]["odoo"], 7.5)
        self.assertEqual(projects["Outro Projeto"], {"projeto": "Outro Projeto", "jira": 0.0, "odoo": 0.5, "diff": 0.5})
        # ordem: faltando primeiro, manual por último
        self.assertEqual(record["items"][0]["status"], "faltando")
        self.assertEqual(record["items"][-1]["status"], "manual")

    def test_department_routing_expected_project(self):
        issue = make_issue(
            key="TAD-965", project_key="TAD", project_name="ITPR | Tarefas Avulsas",
            extra_fields={"customfield_10052": {"value": "FI - Financeiro"}},
        )
        sem_dept = make_issue(key="TAD-970", project_key="TAD", project_name="ITPR | Tarefas Avulsas")
        closing = self.build(
            [make_worklog(wid="1", issue_id="901", seconds=3600, started="2026-06-08T09:00:00.000-0300"),
             make_worklog(wid="2", issue_id="902", seconds=1800, started="2026-06-08T10:00:00.000-0300")],
            [line(100, "1", 1.0, project=60)],
            issues={"901": issue, "902": sem_dept},
            department_field="Departamento Dexterity",
            department_projects=["TAD"],
            department_concat=True,
        )
        record = closing.run("dia", date(2026, 6, 8))
        self.assertEqual(record["counts"], {"ok": 1, "sem_destino": 1})
        item = record["items"][0]
        self.assertEqual(item["issue"], "TAD-970")
        self.assertIn("vazio", item["detalhe"])
        self.assertEqual(item["projeto_esperado"], "(sem destino) ITPR | Tarefas Avulsas")
        projects = {p["projeto"]: p for p in record["projects"]}
        self.assertEqual(projects["ITPR | Tarefas Avulsas | FI - Financeiro"]["diff"], 0.0)

    def test_respects_project_filter(self):
        closing = self.build(
            [make_worklog(wid="1", seconds=3600, started="2026-06-08T09:00:00.000-0300")],
            [],
            jira_project_keys=["XYZ"],
        )
        record = closing.run("semana", self.REF)
        self.assertEqual(record["n_worklogs"], 0)
        self.assertTrue(record["ok"])


class ClosingStorageTest(unittest.TestCase):
    def test_rerun_replaces_same_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "closings.json"
            storage.save_closing(path, {"kind": "dia", "start": "2026-06-08", "ok": False})
            storage.save_closing(path, {"kind": "semana", "start": "2026-06-08", "ok": True})
            storage.save_closing(path, {"kind": "dia", "start": "2026-06-08", "ok": True})
            closings = storage.read_closings(path)
            self.assertEqual([c["id"] for c in closings], ["dia:2026-06-08", "semana:2026-06-08"])
            self.assertTrue(closings[0]["ok"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))[0]["id"], "dia:2026-06-08")


if __name__ == "__main__":
    unittest.main()
