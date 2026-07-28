"""Criação de issues no GitHub para o botão "Reportar problema" do app.

Usa apenas urllib (stdlib). Requer um token com permissão de escrita em
Issues no repositório (fine-grained personal access token). O token vive na
variável de ambiente GITHUB_TOKEN, gerenciada pela tela de Conexões.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_BUG_REPO = "dfg-dexterity/syncjiraodoo"


class GitHubError(RuntimeError):
    pass


def create_issue(
    repo: str,
    token: str,
    title: str,
    body: str,
    labels: tuple[str, ...] = ("bug",),
    timeout: int = 30,
) -> str:
    """Cria a issue e devolve a URL dela (html_url)."""
    url = f"https://api.github.com/repos/{repo}/issues"
    payload = json.dumps({"title": title, "body": body, "labels": list(labels)}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "syncjiraodoo-bug-report",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise GitHubError(f"GitHub respondeu HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"erro de rede ao falar com o GitHub: {exc.reason}") from exc
    html_url = data.get("html_url", "")
    if not html_url:
        raise GitHubError("GitHub não devolveu a URL da issue criada")
    return html_url
