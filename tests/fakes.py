"""Dublês em memória dos clientes Jira e Odoo para os testes."""

from __future__ import annotations

import itertools


class FakeOdoo:
    """Implementa o subconjunto da interface de OdooClient usado pelo sync.

    Suporta os operadores de domínio usados pelo motor: '=', '=ilike' e
    'like' (substring, como no ORM do Odoo).
    """

    def __init__(self, records: dict[str, list[dict]] | None = None):
        self.data: dict[str, list[dict]] = records or {}
        self._seq = itertools.count(1000)

    @staticmethod
    def _match(record: dict, domain: list) -> bool:
        for field_name, op, value in domain:
            actual = record.get(field_name)
            if op == "=":
                if actual != value:
                    return False
            elif op == "=ilike":
                if str(actual).lower() != str(value).lower():
                    return False
            elif op == "like":
                if str(value) not in str(actual):
                    return False
            else:
                raise NotImplementedError(f"operador não suportado no fake: {op}")
        return True

    def search_read(self, model, domain, fields, limit=None, include_archived=False):
        rows = [
            dict(r)
            for r in self.data.get(model, [])
            if self._match(r, domain) and (include_archived or r.get("active", True))
        ]
        return rows[:limit] if limit else rows

    def create(self, model, vals):
        record = dict(vals, id=next(self._seq))
        self.data.setdefault(model, []).append(record)
        return record["id"]

    def write(self, model, ids, vals):
        for record in self.data.get(model, []):
            if record["id"] in ids:
                record.update(vals)
        return True

    def unlink(self, model, ids):
        self.data[model] = [r for r in self.data.get(model, []) if r["id"] not in ids]
        return True


class FakeJira:
    def __init__(self, worklogs: list[dict], issues: dict[str, dict], deleted: list[int] = ()):
        self.worklogs = {str(w["id"]): w for w in worklogs}
        self.issues = issues
        self.deleted = list(deleted)

    def updated_worklog_ids(self, since_ms):
        return [int(w_id) for w_id in self.worklogs]

    def deleted_worklog_ids(self, since_ms):
        return list(self.deleted)

    def get_worklogs(self, ids):
        return [self.worklogs[str(i)] for i in ids]

    def get_issue(self, issue_id, fields=("summary", "project")):
        return self.issues[str(issue_id)]


def make_worklog(
    wid="27279",
    issue_id="35772",
    seconds=1200,
    started="2026-06-08T17:00:00.000-0300",
    comment_text="Feito",
    author_email="diego@dexterityit.com.br",
    account_id="712020:3a98a142",
):
    author = {"accountId": account_id, "displayName": "Diego Gozer"}
    if author_email:
        author["emailAddress"] = author_email
    comment = None
    if comment_text:
        comment = {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": comment_text}]}
            ],
        }
    return {
        "id": str(wid),
        "issueId": str(issue_id),
        "author": author,
        "comment": comment,
        "started": started,
        "timeSpentSeconds": seconds,
    }


def make_issue(key="CDV-331", summary="Reunião de preparação", project_key="CDV",
               project_name="Greenfield | Casa dos Ventos"):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "project": {"key": project_key, "name": project_name},
        },
    }
