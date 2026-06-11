"""Tabela de-para (mapping.json) entre Jira e Odoo.

Formato do arquivo — versionável, sem segredos:

{
  "restrict_to_mapped_projects": false,
  "projects": [
    {"odoo": "Casa dos Ventos", "jira": ["CDV", "CCDV"]}
  ],
  "users": [
    {"jira": "diego@dexterityit.com.br", "odoo": "diego@dexterityit.com.br",
     "nome": "Diego Gozer"}
  ]
}

- Uma linha de projeto aceita VÁRIAS keys Jira apontando para o mesmo
  projeto Odoo (N:1).
- Em "users", a coluna "jira" aceita e-mail ou accountId (necessário quando
  o perfil Atlassian oculta o e-mail).
- "restrict_to_mapped_projects": quando true, só os projetos mapeados são
  sincronizados (a menos que JIRA_PROJECT_KEYS/--projects diga outra coisa).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Mapping:
    restrict_to_mapped_projects: bool = False
    projects: list[dict] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Mapping":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            restrict_to_mapped_projects=bool(data.get("restrict_to_mapped_projects", False)),
            projects=list(data.get("projects", [])),
            users=list(data.get("users", [])),
        )

    def save(self, path: Path) -> None:
        data = {
            "restrict_to_mapped_projects": self.restrict_to_mapped_projects,
            "projects": self.projects,
            "users": self.users,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def project_map(self) -> dict[str, str]:
        """Achata as linhas em {KEY_JIRA: nome do projeto Odoo}."""
        result: dict[str, str] = {}
        for row in self.projects:
            name = str(row.get("odoo", "")).strip()
            for key in row.get("jira", []):
                key = str(key).strip().upper()
                if key and name:
                    result[key] = name
        return result

    def employee_map(self) -> dict[str, str]:
        """Achata as linhas em {e-mail ou accountId Jira: e-mail Odoo}."""
        result: dict[str, str] = {}
        for row in self.users:
            jira = str(row.get("jira", "")).strip()
            odoo = str(row.get("odoo", "")).strip()
            if jira and odoo:
                result[jira] = odoo
                if "@" in jira:
                    result[jira.lower()] = odoo
        return result

    def validate(self) -> list[str]:
        errors: list[str] = []
        seen_keys: dict[str, str] = {}
        for i, row in enumerate(self.projects, start=1):
            name = str(row.get("odoo", "")).strip()
            keys = [str(k).strip().upper() for k in row.get("jira", []) if str(k).strip()]
            if not name:
                errors.append(f"projetos, linha {i}: nome do projeto Odoo vazio")
            if not keys:
                errors.append(f"projetos, linha {i}: nenhuma key Jira informada")
            for key in keys:
                if key in seen_keys and seen_keys[key] != name:
                    errors.append(
                        f"projetos: key Jira {key} mapeada para dois projetos Odoo "
                        f"diferentes ('{seen_keys[key]}' e '{name}')"
                    )
                seen_keys[key] = name

        seen_users: set[str] = set()
        for i, row in enumerate(self.users, start=1):
            jira = str(row.get("jira", "")).strip()
            odoo = str(row.get("odoo", "")).strip()
            if not jira or not odoo:
                errors.append(f"usuários, linha {i}: preencha os dois lados do de-para")
                continue
            if jira.lower() in seen_users:
                errors.append(f"usuários: '{jira}' aparece em mais de uma linha")
            seen_users.add(jira.lower())
        return errors
