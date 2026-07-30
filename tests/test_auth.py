from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from sync_jira_odoo.auth import (
    SessionStore,
    add_user,
    load_users,
    remove_user,
    verify_user,
)


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "users.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_verify_user(self):
        add_user(self.path, "Ana@Empresa.com.br", "senha-forte-123", admin=True)
        record = verify_user(self.path, "ana@empresa.com.br", "senha-forte-123")
        self.assertIsNotNone(record)
        self.assertTrue(record["admin"])
        self.assertIsNone(verify_user(self.path, "ana@empresa.com.br", "senha-errada"))
        self.assertIsNone(verify_user(self.path, "outra@empresa.com.br", "senha-forte-123"))

    def test_password_never_stored_in_plaintext(self):
        add_user(self.path, "a@b.com", "senha-forte-123")
        self.assertNotIn("senha-forte-123", self.path.read_text(encoding="utf-8"))
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_short_password_rejected(self):
        with self.assertRaises(ValueError):
            add_user(self.path, "a@b.com", "curta")

    def test_cannot_remove_last_admin(self):
        add_user(self.path, "chefe@b.com", "senha-forte-123", admin=True)
        add_user(self.path, "colega@b.com", "senha-forte-123")
        with self.assertRaises(ValueError):
            remove_user(self.path, "chefe@b.com")
        remove_user(self.path, "colega@b.com")
        self.assertEqual(list(load_users(self.path)), ["chefe@b.com"])

    def test_sessions_expire(self):
        store = SessionStore(ttl=0)
        token = store.get(store.create("a@b.com"))
        self.assertIsNone(token)
        store = SessionStore(ttl=60)
        token = store.create("a@b.com")
        self.assertEqual(store.get(token), "a@b.com")
        store.drop(token)
        self.assertIsNone(store.get(token))


if __name__ == "__main__":
    unittest.main()
