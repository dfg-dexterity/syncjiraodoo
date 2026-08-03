"""CLI: python -m sync_jira_odoo [opções]"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import storage
from .config import Config, ConfigError
from .envfile import DEFAULT_ENV_FILE, load_env_file
from .jira_client import JiraClient, JiraError
from .logsetup import DEFAULT_RUN_LOG_FILE, configure_logging
from .mapping import Mapping
from .odoo_client import OdooClient, OdooError
from .sync import SyncEngine, connection_report

log = logging.getLogger("sync_jira_odoo")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_jira_odoo",
        description="Sincroniza worklogs do Jira (Clockwork) para timesheets do Odoo.",
    )
    parser.add_argument(
        "--test-connection",
        action="store_true",
        help="só valida as credenciais do Odoo e do Jira e sai",
    )
    parser.add_argument(
        "--since",
        metavar="ISO",
        help="sincroniza worklogs alterados desde esta data/hora (ex.: 2026-06-01); "
        "padrão: última execução registrada no arquivo de estado, ou "
        f"{storage.DEFAULT_LOOKBACK_DAYS} dias atrás",
    )
    parser.add_argument(
        "--projects",
        metavar="KEYS",
        help="sobrescreve JIRA_PROJECT_KEYS (lista de keys separadas por vírgula)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="mostra o que seria feito sem gravar nada no Odoo",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="remove no Odoo os timesheets de worklogs apagados no Jira",
    )
    parser.add_argument(
        "--mapping-file",
        default=os.environ.get("MAPPING_FILE", "mapping.json"),
        help="tabela de-para Jira/Odoo (padrão: mapping.json)",
    )
    parser.add_argument(
        "--state-file",
        default=storage.DEFAULT_STATE_FILE,
        help=f"arquivo local de estado incremental (padrão: {storage.DEFAULT_STATE_FILE})",
    )
    parser.add_argument(
        "--history-file",
        default=storage.DEFAULT_HISTORY_FILE,
        help=f"histórico de execuções para o monitor (padrão: {storage.DEFAULT_HISTORY_FILE})",
    )
    parser.add_argument(
        "--import-log-file",
        default=storage.DEFAULT_IMPORT_LOG_FILE,
        help="log persistente dos timesheets importados no Odoo "
        f"(padrão: {storage.DEFAULT_IMPORT_LOG_FILE}; dry-run não grava)",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_RUN_LOG_FILE,
        help="log completo da execução, rotativo (padrão: "
        f"{DEFAULT_RUN_LOG_FILE}; use '' para desativar)",
    )
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help=f"arquivo de credenciais carregado automaticamente (padrão: {DEFAULT_ENV_FILE})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log detalhado")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(verbose=args.verbose, log_file=args.log_file or None)
    load_env_file(args.env_file)

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    mapping = Mapping.load(Path(args.mapping_file))
    errors = mapping.validate()
    if errors:
        for error in errors:
            log.error("mapping inválido: %s", error)
        return 2
    cfg.apply_mapping(mapping)

    if args.projects:
        cfg.jira_project_keys = [k.strip().upper() for k in args.projects.split(",") if k.strip()]

    history_path = Path(args.history_file)
    try:
        if args.test_connection:
            print(connection_report(cfg))
            return 0

        state_path = Path(args.state_file)
        since = storage.load_since(args.since, state_path)
        run_started = datetime.now(timezone.utc)

        odoo = OdooClient(cfg.odoo_url, cfg.odoo_db, cfg.odoo_user, cfg.odoo_api_key)
        jira = JiraClient(cfg.jira_url, cfg.jira_user, cfg.jira_api_token)
        engine = SyncEngine(jira, odoo, cfg)
        result = engine.run(since, dry_run=args.dry_run, delete=args.delete)

        log.info(
            "fim: %d criados, %d atualizados, %d pulados, %d removidos, %d avisos, %d erros%s",
            result.created,
            result.updated,
            result.skipped,
            result.deleted,
            len(result.warnings),
            len(result.errors),
            " (dry-run, nada gravado)" if args.dry_run else "",
        )
        storage.append_history(
            history_path,
            {
                "finished_utc": datetime.now(timezone.utc).isoformat(),
                "since": since.isoformat(),
                "dry_run": args.dry_run,
                "delete": args.delete,
                "created": result.created,
                "updated": result.updated,
                "skipped": result.skipped,
                "deleted": result.deleted,
                "warnings": result.warnings,
                "errors": result.errors,
                "ok": True,
            },
        )
        if not args.dry_run:
            storage.append_import_items(Path(args.import_log_file), result.items)
            storage.save_state(state_path, run_started)
        return 0
    except (OdooError, JiraError) as exc:
        log.error("%s", exc)
        if not args.test_connection:
            storage.append_history(
                history_path,
                {
                    "finished_utc": datetime.now(timezone.utc).isoformat(),
                    "dry_run": args.dry_run,
                    "ok": False,
                    "error": str(exc),
                },
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
