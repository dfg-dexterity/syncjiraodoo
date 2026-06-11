"""Configuration loading for syncjiraodoo.

Reads settings from environment variables, optionally seeded from a local
``.env`` file. A tiny hand-rolled ``.env`` parser is used so the project has no
runtime dependency on python-dotenv just to test a connection.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Load ``KEY=VALUE`` pairs from a .env file into ``os.environ``.

    Existing environment variables are never overwritten, so real env vars
    (e.g. in CI) take precedence over the file. Lines that are blank or start
    with ``#`` are ignored, as are values wrapped in matching quotes.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class OdooConfig:
    url: str
    db: str
    username: str
    api_key: str

    @classmethod
    def from_env(cls) -> "OdooConfig":
        missing: list[str] = []
        values: dict[str, str] = {}
        for field_name, env_name in (
            ("url", "ODOO_URL"),
            ("db", "ODOO_DB"),
            ("username", "ODOO_USERNAME"),
            ("api_key", "ODOO_API_KEY"),
        ):
            value = os.environ.get(env_name, "").strip()
            if not value:
                missing.append(env_name)
            values[field_name] = value
        if missing:
            raise ConfigError(
                "Missing Odoo configuration: " + ", ".join(missing)
            )
        return cls(
            url=values["url"].rstrip("/"),
            db=values["db"],
            username=values["username"],
            api_key=values["api_key"],
        )


@dataclass(frozen=True)
class JiraConfig:
    url: str
    email: str
    api_token: str

    @classmethod
    def from_env(cls) -> "JiraConfig":
        missing: list[str] = []
        values: dict[str, str] = {}
        for field_name, env_name in (
            ("url", "JIRA_URL"),
            ("email", "JIRA_EMAIL"),
            ("api_token", "JIRA_API_TOKEN"),
        ):
            value = os.environ.get(env_name, "").strip()
            if not value:
                missing.append(env_name)
            values[field_name] = value
        if missing:
            raise ConfigError(
                "Missing Jira configuration: " + ", ".join(missing)
            )
        return cls(
            url=values["url"].rstrip("/"),
            email=values["email"],
            api_token=values["api_token"],
        )
