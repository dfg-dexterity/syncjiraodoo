"""CLI: python -m sync_jira_odoo [opções]"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config, ConfigError
from .jira_client import JiraClient, JiraError
from .odoo_client import OdooClient, OdooError
from .sync import SyncEngine

log = logging.getLogger("sync_jira_odoo")

DEFAULT_STATE_FILE = ".sync_state.json"
DEFAULT_LOOKBACK_DAYS = 7


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
        f"{DEFAULT_LOOKBACK_DAYS} dias atrás",
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
        "--state-file",
        default=DEFAULT_STATE_FILE,
        help=f"arquivo local de estado incremental (padrão: {DEFAULT_STATE_FILE})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log detalhado")
    return parser


def load_since(args: argparse.Namespace, state_path: Path) -> datetime:
    if args.since:
        dt = datetime.fromisoformat(args.since)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if state_path.exists():
        state = json.loads(state_path.read_text())
        return datetime.fromisoformat(state["last_sync_utc"])
    return datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def test_connection(cfg: Config) -> int:
    odoo = OdooClient(cfg.odoo_url, cfg.odoo_db, cfg.odoo_user, cfg.odoo_api_key)
    version = odoo.version()
    print(f"Odoo: {version.get('server_version')} (série {version.get('server_serie')})")
    print(f"Odoo authenticate(): OK, uid={odoo.uid}")

    jira = JiraClient(cfg.jira_url, cfg.jira_user, cfg.jira_api_token)
    me = jira.myself()
    print(f"Jira: autenticado como {me.get('displayName')} ({me.get('emailAddress')})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.projects:
        cfg.jira_project_keys = [k.strip().upper() for k in args.projects.split(",") if k.strip()]

    try:
        if args.test_connection:
            return test_connection(cfg)

        state_path = Path(args.state_file)
        since = load_since(args, state_path)
        run_started = datetime.now(timezone.utc)

        odoo = OdooClient(cfg.odoo_url, cfg.odoo_db, cfg.odoo_user, cfg.odoo_api_key)
        jira = JiraClient(cfg.jira_url, cfg.jira_user, cfg.jira_api_token)
        engine = SyncEngine(jira, odoo, cfg)
        result = engine.run(since, dry_run=args.dry_run, delete=args.delete)

        log.info(
            "fim: %d criados, %d atualizados, %d pulados, %d removidos, %d avisos%s",
            result.created,
            result.updated,
            result.skipped,
            result.deleted,
            len(result.warnings),
            " (dry-run, nada gravado)" if args.dry_run else "",
        )
        if not args.dry_run:
            state_path.write_text(
                json.dumps({"last_sync_utc": run_started.isoformat()}, indent=2)
            )
        return 0
    except (OdooError, JiraError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
