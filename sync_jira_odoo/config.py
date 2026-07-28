"""Configuração via variáveis de ambiente.

Nenhuma credencial é lida de arquivos do repositório: tudo vem do ambiente
(ou de um arquivo .env carregado externamente, fora do controle de versão).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"variável de ambiente obrigatória ausente: {name}")
    return value


def _json_map(name: str) -> dict[str, str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{name} não é JSON válido: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{name} deve ser um objeto JSON {{chave: valor}}")
    return {str(k): str(v) for k, v in data.items()}


@dataclass
class Config:
    odoo_url: str
    odoo_db: str
    odoo_user: str
    odoo_api_key: str
    jira_url: str
    jira_user: str
    jira_api_token: str
    # Filtro opcional: só sincroniza worklogs destes projetos Jira (keys).
    jira_project_keys: list[str] = field(default_factory=list)
    # Mapeia key de projeto Jira → nome exato do projeto no Odoo.
    # Projetos não mapeados são criados/localizados pelo marcador "[KEY]".
    project_map: dict[str, str] = field(default_factory=dict)
    # Mapeia accountId (ou e-mail) do autor no Jira → e-mail do funcionário
    # no Odoo, para quando o Jira esconde o e-mail (configuração de
    # privacidade do perfil).
    employee_map: dict[str, str] = field(default_factory=dict)
    # E-mail de funcionário usado quando o autor não pôde ser resolvido.
    default_employee_email: str = ""
    # Roteamento por departamento (ver Mapping.department_routing): nos
    # projetos Jira listados, o projeto Odoo vem do valor deste campo da issue.
    department_field: str = ""
    department_projects: list[str] = field(default_factory=list)
    department_map: dict[str, str] = field(default_factory=dict)

    def apply_mapping(self, mapping) -> None:
        """Mescla uma tabela de-para (mapping.json); o arquivo tem precedência
        sobre os mapas vindos de variáveis de ambiente."""
        self.project_map.update(mapping.project_map())
        self.employee_map.update(mapping.employee_map())
        self.department_field = mapping.department_field()
        self.department_projects = mapping.department_projects()
        self.department_map = mapping.department_map()
        if mapping.restrict_to_mapped_projects and not self.jira_project_keys:
            self.jira_project_keys = sorted(
                set(mapping.project_map()) | set(self.department_projects)
            )

    @classmethod
    def from_env(cls) -> "Config":
        keys_raw = os.environ.get("JIRA_PROJECT_KEYS", "")
        keys = [k.strip().upper() for k in keys_raw.split(",") if k.strip()]
        return cls(
            odoo_url=_require("ODOO_URL").rstrip("/"),
            odoo_db=_require("ODOO_DB"),
            odoo_user=_require("ODOO_USER"),
            odoo_api_key=_require("ODOO_API_KEY"),
            jira_url=_require("JIRA_URL").rstrip("/"),
            jira_user=_require("JIRA_USER"),
            jira_api_token=_require("JIRA_API_TOKEN"),
            jira_project_keys=keys,
            project_map={k.upper(): v for k, v in _json_map("JIRA_ODOO_PROJECT_MAP").items()},
            employee_map=_json_map("JIRA_ODOO_EMPLOYEE_MAP"),
            default_employee_email=os.environ.get("DEFAULT_EMPLOYEE_EMAIL", "").strip(),
        )
