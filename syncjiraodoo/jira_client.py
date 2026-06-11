"""Minimal Jira Cloud REST client.

Uses basic auth (email + API token) and only implements the ``/myself``
endpoint needed to verify connectivity. Built on the stdlib ``urllib`` to keep
the project dependency-free.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import JiraConfig


@dataclass
class JiraConnectionResult:
    ok: bool
    detail: str
    account_id: str | None = None
    display_name: str | None = None


class JiraClient:
    def __init__(self, config: JiraConfig, timeout: float = 15.0) -> None:
        self.config = config
        self.timeout = timeout

    def _auth_header(self) -> str:
        raw = f"{self.config.email}:{self.config.api_token}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def test_connection(self) -> JiraConnectionResult:
        """Call ``GET /rest/api/3/myself`` to verify auth and reachability."""
        url = f"{self.config.url}/rest/api/3/myself"
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", self._auth_header())
        request.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return JiraConnectionResult(
                ok=True,
                detail="Authenticated successfully.",
                account_id=payload.get("accountId"),
                display_name=payload.get("displayName"),
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                detail = (
                    f"Authentication rejected (HTTP {exc.code}). Check "
                    "JIRA_EMAIL and JIRA_API_TOKEN."
                )
            else:
                detail = f"Jira returned HTTP {exc.code}: {exc.reason}"
            return JiraConnectionResult(ok=False, detail=detail)
        except urllib.error.URLError as exc:
            return JiraConnectionResult(
                ok=False,
                detail=f"Could not reach Jira at {self.config.url}: {exc.reason}",
            )
