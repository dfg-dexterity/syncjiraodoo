from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from sync_jira_odoo.envfile import load_env_file, save_env_file


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"

    def tearDown(self):
        for key in ("TESTE_SJO_A", "TESTE_SJO_B", "TESTE_SJO_C"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_roundtrip_and_parsing(self):
        self.path.write_text(
            "# comentário\n"
            "TESTE_SJO_A=valor simples\n"
            'export TESTE_SJO_B="com aspas"\n'
            "\n",
            encoding="utf-8",
        )
        values = load_env_file(self.path)
        self.assertEqual(values["TESTE_SJO_A"], "valor simples")
        self.assertEqual(values["TESTE_SJO_B"], "com aspas")
        self.assertEqual(os.environ["TESTE_SJO_A"], "valor simples")

    def test_environment_wins_unless_override(self):
        os.environ["TESTE_SJO_A"] = "do shell"
        self.path.write_text("TESTE_SJO_A=do arquivo\n", encoding="utf-8")
        load_env_file(self.path)
        self.assertEqual(os.environ["TESTE_SJO_A"], "do shell")
        load_env_file(self.path, override=True)
        self.assertEqual(os.environ["TESTE_SJO_A"], "do arquivo")

    def test_save_preserves_comments_and_other_keys(self):
        self.path.write_text(
            "# credenciais\nTESTE_SJO_A=antigo\nTESTE_SJO_B=fica\n", encoding="utf-8"
        )
        save_env_file(self.path, {"TESTE_SJO_A": "novo", "TESTE_SJO_C": "criado"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# credenciais", text)
        self.assertIn("TESTE_SJO_A=novo", text)
        self.assertIn("TESTE_SJO_B=fica", text)
        self.assertIn("TESTE_SJO_C=criado", text)

    def test_save_sets_owner_only_permission(self):
        save_env_file(self.path, {"TESTE_SJO_A": "x"})
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
