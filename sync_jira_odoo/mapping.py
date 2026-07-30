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
    # Roteamento por departamento: para os projetos Jira listados, o projeto
    # Odoo é decidido por um campo da issue (ex.: "Departamento Dexterity"),
    # não pela key. Formato:
    #   {"field": "Departamento Dexterity", "projects": ["TAV", "TADM"],
    #    "map": [{"departamento": "Financeiro", "odoo": "Adm | Financeiro"}]}
    department_routing: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Mapping":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            restrict_to_mapped_projects=bool(data.get("restrict_to_mapped_projects", False)),
            projects=list(data.get("projects", [])),
            users=list(data.get("users", [])),
            department_routing=dict(data.get("department_routing", {})),
        )

    def save(self, path: Path) -> None:
        data = {
            "restrict_to_mapped_projects": self.restrict_to_mapped_projects,
            "projects": self.projects,
            "users": self.users,
            "department_routing": self.department_routing,
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

    def department_field(self) -> str:
        return str(self.department_routing.get("field", "")).strip()

    def department_projects(self) -> list[str]:
        return [
            str(k).strip().upper()
            for k in self.department_routing.get("projects", [])
            if str(k).strip()
        ]

    def department_map(self) -> dict[str, str]:
        """Achata as linhas em {valor do campo (minúsculo): projeto Odoo}."""
        result: dict[str, str] = {}
        for row in self.department_routing.get("map", []):
            dept = str(row.get("departamento", "")).strip()
            odoo = str(row.get("odoo", "")).strip()
            if dept and odoo:
                result[dept.lower()] = odoo
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

        routing = self.department_routing
        if routing:
            rows = list(routing.get("map", []))
            if self.department_projects() or rows:
                if not self.department_field():
                    errors.append(
                        "roteamento por departamento: informe o nome do campo no Jira"
                    )
                if not self.department_projects():
                    errors.append(
                        "roteamento por departamento: informe as keys dos projetos Jira"
                    )
            seen_depts: set[str] = set()
            for i, row in enumerate(rows, start=1):
                dept = str(row.get("departamento", "")).strip()
                odoo = str(row.get("odoo", "")).strip()
                if not dept or not odoo:
                    errors.append(
                        f"roteamento por departamento, linha {i}: preencha os dois lados"
                    )
                    continue
                if dept.lower() in seen_depts:
                    errors.append(
                        f"roteamento por departamento: '{dept}' aparece em mais de uma linha"
                    )
                seen_depts.add(dept.lower())
        return errors
