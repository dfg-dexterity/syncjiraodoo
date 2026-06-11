"""Command-line entry point for syncjiraodoo."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import ConfigError, JiraConfig, OdooConfig, load_dotenv
from .jira_client import JiraClient
from .odoo_client import OdooClient

_OK = "✅"
_FAIL = "❌"


def _test_connections() -> int:
    """Ping Jira and Odoo independently. Returns a process exit code."""
    load_dotenv()
    all_ok = True

    print("Testing connections...\n")

    # --- Jira ---
    print("Jira (Atlassian Cloud):")
    try:
        jira = JiraClient(JiraConfig.from_env())
        result = jira.test_connection()
        if result.ok:
            who = result.display_name or "unknown user"
            print(f"  {_OK} {result.detail} Logged in as {who} "
                  f"({result.account_id}).")
        else:
            all_ok = False
            print(f"  {_FAIL} {result.detail}")
    except ConfigError as exc:
        all_ok = False
        print(f"  {_FAIL} {exc}")

    print()

    # --- Odoo ---
    print("Odoo (XML-RPC):")
    try:
        odoo = OdooClient(OdooConfig.from_env())
        result = odoo.test_connection()
        prefix = _OK if result.ok else _FAIL
        version = f" Server {result.server_version}." if result.server_version else ""
        print(f"  {prefix} {result.detail}{version}")
        if not result.ok:
            all_ok = False
    except ConfigError as exc:
        all_ok = False
        print(f"  {_FAIL} {exc}")

    print()
    if all_ok:
        print(f"{_OK} All connections OK.")
        return 0
    print(f"{_FAIL} One or more connections failed.")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syncjiraodoo",
        description="Sync utilities between Jira and Odoo.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--test-connection",
        action="store_true",
        help="Verify connectivity and auth for both Jira and Odoo, then exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.test_connection:
        return _test_connections()

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
