"""Configuração de log compartilhada entre o CLI e a interface web.

Além do stderr (comportamento original), grava o log completo da execução —
cada "criando timesheet", aviso e erro — em um arquivo rotativo local, para
que qualquer administrador consiga auditar depois o que a integração fez,
mesmo sem ter visto o terminal.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_RUN_LOG_FILE = ".sync_run.log"
_MAX_BYTES = 2_000_000
_BACKUPS = 3


def configure_logging(verbose: bool = False, log_file: str | Path | None = None) -> None:
    """Loga em stderr e, se `log_file` for informado, também no arquivo
    (rotação automática: 2 MB por arquivo, 3 backups)."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_file:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
