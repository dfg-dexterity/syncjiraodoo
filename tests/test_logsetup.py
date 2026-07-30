from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from sync_jira_odoo.logsetup import configure_logging


class LogSetupTest(unittest.TestCase):
    def setUp(self):
        self._saved_handlers = list(logging.getLogger().handlers)
        self._saved_level = logging.getLogger().level

    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            handler.close()
            root.removeHandler(handler)
        for handler in self._saved_handlers:
            root.addHandler(handler)
        root.setLevel(self._saved_level)

    def test_writes_run_log_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            configure_logging(log_file=path)
            logging.getLogger("sync_jira_odoo").info("criando timesheet: TESTE-1 (0.5h)")
            for handler in logging.getLogger().handlers:
                handler.flush()
            content = path.read_text(encoding="utf-8")
            self.assertIn("criando timesheet: TESTE-1 (0.5h)", content)
            self.assertIn("INFO", content)

    def test_without_file_only_stderr_handler(self):
        configure_logging(log_file=None)
        handlers = logging.getLogger().handlers
        self.assertEqual(len(handlers), 1)

    def test_reconfigure_does_not_duplicate_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            configure_logging(log_file=path)
            configure_logging(log_file=path)
            self.assertEqual(len(logging.getLogger().handlers), 2)  # stderr + arquivo


if __name__ == "__main__":
    unittest.main()
