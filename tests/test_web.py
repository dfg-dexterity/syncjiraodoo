from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from sync_jira_odoo import storage
from sync_jira_odoo.mapping import Mapping
from sync_jira_odoo.web import App, SyncRunner


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.mapping_path = base / "mapping.json"
        Mapping(projects=[{"odoo": "Casa dos Ventos", "jira": ["CDV"]}]).save(self.mapping_path)
        storage.append_history(base / "history.json", {"ok": True, "created": 3})

        self.runner = SyncRunner(self.mapping_path, base / "state.json", base / "history.json")
        self.server = App(("127.0.0.1", 0), self.runner)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")

    def test_index_serves_html(self):
        with urllib.request.urlopen(self.base_url + "/") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("De-para de projetos", resp.read().decode())

    def test_state_returns_mapping_and_history(self):
        status, state = self._request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["mapping"]["projects"][0]["jira"], ["CDV"])
        self.assertEqual(state["history"][0]["created"], 3)
        self.assertFalse(state["running"])

    def test_save_mapping_roundtrip(self):
        body = {
            "restrict_to_mapped_projects": True,
            "projects": [{"odoo": "Fiagril", "jira": ["FIAG", "FB"]}],
            "users": [{"jira": "a@b.com", "odoo": "a@b.com", "nome": ""}],
        }
        status, data = self._request("/api/mapping", body)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        saved = Mapping.load(self.mapping_path)
        self.assertTrue(saved.restrict_to_mapped_projects)
        self.assertEqual(saved.project_map(), {"FIAG": "Fiagril", "FB": "Fiagril"})

    def test_invalid_mapping_returns_400_with_errors(self):
        body = {"projects": [{"odoo": "", "jira": ["X"]}], "users": []}
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/mapping", body)
        self.assertEqual(ctx.exception.code, 400)
        errors = json.loads(ctx.exception.read())["errors"]
        self.assertEqual(len(errors), 1)
        saved = Mapping.load(self.mapping_path)  # arquivo não foi sobrescrito
        self.assertEqual(saved.project_map(), {"CDV": "Casa dos Ventos"})

    def test_sync_rejected_while_running(self):
        executed = threading.Event()
        self.runner._execute = lambda *args: executed.wait(5)
        status, data = self._request("/api/sync", {"dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/sync", {"dry_run": True})
        self.assertEqual(ctx.exception.code, 409)
        executed.set()


if __name__ == "__main__":
    unittest.main()
