from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

from sync_jira_odoo import auth, storage
from sync_jira_odoo.mapping import Mapping
from sync_jira_odoo.web import App, SyncRunner

ADMIN = "admin@exemplo.com.br"
SENHA = "senha-forte-123"


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.mapping_path = base / "mapping.json"
        Mapping(projects=[{"odoo": "Casa dos Ventos", "jira": ["CDV"]}]).save(self.mapping_path)
        storage.append_history(base / "history.json", {"ok": True, "created": 3})

        self.import_log_path = base / "import_log.jsonl"
        self.env_path = base / ".env"
        self.users_path = base / "users.json"
        auth.add_user(self.users_path, ADMIN, SENHA, admin=True)
        self.runner = SyncRunner(
            self.mapping_path,
            base / "state.json",
            base / "history.json",
            self.import_log_path,
            self.env_path,
        )
        self.server = App(("127.0.0.1", 0), self.runner, self.users_path)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cookie = self._login(ADMIN, SENHA)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _login(self, email, senha):
        req = urllib.request.Request(
            self.base_url + "/api/login",
            data=json.dumps({"email": email, "senha": senha}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            set_cookie = resp.getheader("Set-Cookie") or ""
        self.cookie_header = set_cookie
        return set_cookie.split(";")[0]

    def _request(self, path, body=None, cookie="use-session"):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if cookie == "use-session":
            headers["Cookie"] = self.cookie
        elif cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")

    def test_index_serves_app_when_logged_in(self):
        req = urllib.request.Request(self.base_url + "/", headers={"Cookie": self.cookie})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode()
        self.assertIn("Sincronizador de Horas", html)
        self.assertIn("De-para de projetos", html)
        self.assertIn("Simular (não grava nada)", html)

    def test_index_serves_login_when_anonymous(self):
        with urllib.request.urlopen(self.base_url + "/") as resp:
            html = resp.read().decode()
        self.assertIn("Entrar", html)
        self.assertNotIn("De-para de projetos", html)

    def test_api_requires_login(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/state", cookie=None)
        self.assertEqual(ctx.exception.code, 401)

    def test_wrong_password_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._login(ADMIN, "senha-errada-999")
        self.assertEqual(ctx.exception.code, 401)

    def test_setup_blocked_when_users_exist(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/setup", {"email": "x@y.com", "senha": "12345678"}, cookie=None)
        self.assertEqual(ctx.exception.code, 403)

    def test_session_cookie_secure_behind_https_proxy(self):
        req = urllib.request.Request(
            self.base_url + "/api/login",
            data=json.dumps({"email": ADMIN, "senha": SENHA}).encode(),
            headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            set_cookie = resp.getheader("Set-Cookie") or ""
        self.assertIn("Secure", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        # sem o proxy HTTPS, o flag Secure não entra (senão o cookie some em HTTP local)
        self.assertNotIn("Secure", self.cookie_header or "")

    def test_non_admin_cannot_manage_users(self):
        status, data = self._request(
            "/api/users", {"email": "membro@exemplo.com.br", "senha": "senha-forte-123"}
        )
        self.assertTrue(data["ok"])
        member_cookie = self._login("membro@exemplo.com.br", "senha-forte-123")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/users", cookie=member_cookie)
        self.assertEqual(ctx.exception.code, 403)
        status, data = self._request("/api/users", cookie="use-session")
        emails = [u["email"] for u in data["users"]]
        self.assertIn("membro@exemplo.com.br", emails)

    def test_bug_report_creates_github_issue(self):
        os.environ["GITHUB_TOKEN"] = "token-teste"
        try:
            with unittest.mock.patch(
                "sync_jira_odoo.web.create_issue",
                return_value="https://github.com/dfg-dexterity/syncjiraodoo/issues/9",
            ) as fake:
                status, data = self._request(
                    "/api/bug", {"titulo": "horas sumiram", "descricao": "detalhe do problema"}
                )
        finally:
            os.environ.pop("GITHUB_TOKEN", None)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("/issues/9", data["url"])
        repo, token, title, body = fake.call_args[0]
        self.assertEqual(repo, "dfg-dexterity/syncjiraodoo")
        self.assertEqual(title, "[bug] horas sumiram")
        self.assertIn(ADMIN, body)  # quem reportou vai no corpo
        self.assertIn("detalhe do problema", body)
        self.assertIn("Última execução", body)

    def test_bug_report_requires_fields_and_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/bug", {"titulo": "", "descricao": ""})
        self.assertEqual(ctx.exception.code, 400)
        os.environ.pop("GITHUB_TOKEN", None)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/bug", {"titulo": "x", "descricao": "y"})
        self.assertEqual(ctx.exception.code, 400)

    def test_bug_report_requires_login(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/bug", {"titulo": "x", "descricao": "y"}, cookie=None)
        self.assertEqual(ctx.exception.code, 401)

    def test_config_get_masks_secrets(self):
        os.environ["ODOO_API_KEY"] = "segredo-que-nao-volta"
        try:
            status, data = self._request("/api/config")
        finally:
            os.environ.pop("ODOO_API_KEY", None)
        self.assertEqual(status, 200)
        field = data["fields"]["ODOO_API_KEY"]
        self.assertTrue(field["set"])
        self.assertEqual(field["value"], "")

    def test_config_post_saves_env_file_and_environ(self):
        body = {"ODOO_URL": "https://exemplo.odoo.com", "ODOO_DB": "exemplo"}
        try:
            status, data = self._request("/api/config", body)
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(os.environ["ODOO_URL"], "https://exemplo.odoo.com")
            text = self.env_path.read_text(encoding="utf-8")
            self.assertIn("ODOO_URL=https://exemplo.odoo.com", text)
            self.assertIn("ODOO_DB=exemplo", text)
        finally:
            os.environ.pop("ODOO_URL", None)
            os.environ.pop("ODOO_DB", None)

    def test_config_post_empty_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/config", {})
        self.assertEqual(ctx.exception.code, 400)

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

    def test_import_log_endpoint_returns_recent_items_first(self):
        storage.append_import_items(
            self.import_log_path,
            [
                {"acao": "criado", "issue": "CDV-1", "data": "2026-06-01", "horas": 1.0},
                {"acao": "criado", "issue": "CDV-2", "data": "2026-06-02", "horas": 0.5},
            ],
        )
        status, data = self._request("/api/import-log?limit=10")
        self.assertEqual(status, 200)
        issues = [item["issue"] for item in data["items"]]
        self.assertEqual(issues, ["CDV-2", "CDV-1"])  # mais recentes primeiro
        self.assertIn("logged_utc", data["items"][0])

    def test_import_log_empty_when_file_missing(self):
        status, data = self._request("/api/import-log")
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], [])

    def test_index_has_import_log_section(self):
        req = urllib.request.Request(self.base_url + "/", headers={"Cookie": self.cookie})
        with urllib.request.urlopen(req) as resp:
            self.assertIn("Apontamentos importados no Odoo", resp.read().decode())

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


class AuditEndpointsTest(WebTest):
    def test_audit_requires_since(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/audit", {"since": ""})
        self.assertEqual(ctx.exception.code, 400)

    def test_audit_get_returns_empty_initially(self):
        status, data = self._request("/api/audit")
        self.assertEqual(status, 200)
        self.assertIsNone(data["audit"])
        self.assertFalse(data["running"])

    def test_reimport_validates_ids(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/reimport", {"worklogs": []})
        self.assertEqual(ctx.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/reimport", {"worklogs": ["abc"]})
        self.assertEqual(ctx.exception.code, 400)

    def test_audit_requires_login(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/audit", cookie=None)
        self.assertEqual(ctx.exception.code, 401)
