from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sync_jira_odoo import storage
from sync_jira_odoo.mapping import Mapping

from .test_sync import make_config


class MappingTest(unittest.TestCase):
    def test_many_jira_projects_to_one_odoo_project(self):
        mapping = Mapping(projects=[{"odoo": "Casa dos Ventos", "jira": ["CDV", "ccdv"]}])
        self.assertEqual(
            mapping.project_map(),
            {"CDV": "Casa dos Ventos", "CCDV": "Casa dos Ventos"},
        )
        self.assertEqual(mapping.validate(), [])

    def test_employee_map_accepts_email_and_account_id(self):
        mapping = Mapping(
            users=[
                {"jira": "Diego@Dexterityit.com.br", "odoo": "diego@dexterityit.com.br"},
                {"jira": "712020:abc", "odoo": "julian@dexterityit.com.br"},
            ]
        )
        flat = mapping.employee_map()
        self.assertEqual(flat["diego@dexterityit.com.br"], "diego@dexterityit.com.br")
        self.assertEqual(flat["712020:abc"], "julian@dexterityit.com.br")

    def test_validate_catches_conflicts_and_blanks(self):
        mapping = Mapping(
            projects=[
                {"odoo": "A", "jira": ["CDV"]},
                {"odoo": "B", "jira": ["CDV"]},
                {"odoo": "", "jira": []},
            ],
            users=[{"jira": "x@y.com", "odoo": ""}],
        )
        errors = mapping.validate()
        self.assertEqual(len(errors), 4)
        self.assertTrue(any("dois projetos Odoo diferentes" in e for e in errors))

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping.json"
            original = Mapping(
                restrict_to_mapped_projects=True,
                projects=[{"odoo": "Café & Cia", "jira": ["RDF"]}],
                users=[{"jira": "a@b.com", "odoo": "a@b.com", "nome": "Aé"}],
            )
            original.save(path)
            loaded = Mapping.load(path)
            self.assertEqual(loaded, original)

    def test_load_missing_file_is_empty(self):
        mapping = Mapping.load(Path("/tmp/nao-existe-mapping.json"))
        self.assertEqual(mapping.projects, [])
        self.assertFalse(mapping.restrict_to_mapped_projects)


class ApplyMappingTest(unittest.TestCase):
    def test_mapping_file_wins_over_env_maps(self):
        cfg = make_config(
            project_map={"CDV": "Antigo"}, employee_map={"a@b.com": "antigo@b.com"}
        )
        cfg.apply_mapping(
            Mapping(
                projects=[{"odoo": "Novo", "jira": ["CDV"]}],
                users=[{"jira": "a@b.com", "odoo": "novo@b.com"}],
            )
        )
        self.assertEqual(cfg.project_map["CDV"], "Novo")
        self.assertEqual(cfg.employee_map["a@b.com"], "novo@b.com")

    def test_restrict_sets_project_filter_when_empty(self):
        cfg = make_config()
        cfg.apply_mapping(
            Mapping(
                restrict_to_mapped_projects=True,
                projects=[{"odoo": "X", "jira": ["TAD", "RDF"]}],
            )
        )
        self.assertEqual(cfg.jira_project_keys, ["RDF", "TAD"])

    def test_restrict_keeps_explicit_filter(self):
        cfg = make_config(jira_project_keys=["FIAG"])
        cfg.apply_mapping(
            Mapping(restrict_to_mapped_projects=True, projects=[{"odoo": "X", "jira": ["TAD"]}])
        )
        self.assertEqual(cfg.jira_project_keys, ["FIAG"])


class StorageTest(unittest.TestCase):
    def test_history_append_and_trim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            for i in range(5):
                storage.append_history(path, {"run": i}, keep=3)
            history = storage.read_history(path)
            self.assertEqual([r["run"] for r in history], [2, 3, 4])


if __name__ == "__main__":
    unittest.main()
