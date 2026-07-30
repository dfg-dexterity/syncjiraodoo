"""Cliente REST do Jira Cloud usando apenas a stdlib (urllib).

O Clockwork Pro grava os apontamentos como worklogs nativos do Jira, então a
API nativa de worklogs (/rest/api/3/worklog/*) cobre tudo que é registrado
pelo Clockwork — inclusive ajustes manuais e timers.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator


class JiraError(RuntimeError):
    pass


def adf_to_text(node: Any) -> str:
    """Extrai texto puro de um documento ADF (Atlassian Document Format)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(filter(None, (adf_to_text(n) for n in node)))
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return adf_to_text(node.get("content"))
    return ""


class JiraClient:
    WORKLOG_LIST_CHUNK = 1000  # limite da API /worklog/list

    def __init__(self, base_url: str, user: str, api_token: str, timeout: int = 60):
        self._base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{user}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._timeout = timeout
        self._issue_cache: dict[str, dict] = {}

    def _request(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        url = f"{self._base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise JiraError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise JiraError(f"{method} {path} -> erro de rede: {exc.reason}") from exc
        return json.loads(payload) if payload else None

    def myself(self) -> dict:
        return self._request("GET", "/rest/api/3/myself")

    def find_field_id(self, name: str) -> str | None:
        """Localiza o id de um campo (ex.: 'customfield_10052') pelo nome
        visível (ex.: 'Departamento Dexterity'), sem diferenciar maiúsculas."""
        target = name.strip().lower()
        if not target:
            return None
        for field in self._request("GET", "/rest/api/3/field") or []:
            if str(field.get("name", "")).strip().lower() == target:
                return field.get("id")
        return None

    def _changed_worklog_ids(self, endpoint: str, since_ms: int) -> Iterator[int]:
        """Pagina /worklog/updated ou /worklog/deleted a partir de since_ms."""
        since = since_ms
        while True:
            page = self._request("GET", endpoint, params={"since": since})
            for value in page.get("values", []):
                yield value["worklogId"]
            if page.get("lastPage", True):
                return
            since = page["until"]

    def updated_worklog_ids(self, since_ms: int) -> list[int]:
        return list(self._changed_worklog_ids("/rest/api/3/worklog/updated", since_ms))

    def deleted_worklog_ids(self, since_ms: int) -> list[int]:
        return list(self._changed_worklog_ids("/rest/api/3/worklog/deleted", since_ms))

    def get_worklogs(self, ids: list[int]) -> list[dict]:
        worklogs: list[dict] = []
        for start in range(0, len(ids), self.WORKLOG_LIST_CHUNK):
            chunk = ids[start : start + self.WORKLOG_LIST_CHUNK]
            worklogs.extend(self._request("POST", "/rest/api/3/worklog/list", body={"ids": chunk}))
        return worklogs

    def get_issue(self, issue_id: str, fields: tuple[str, ...] = ("summary", "project")) -> dict:
        key = str(issue_id)
        if key not in self._issue_cache:
            self._issue_cache[key] = self._request(
                "GET", f"/rest/api/3/issue/{key}", params={"fields": ",".join(fields)}
            )
        return self._issue_cache[key]
