"""Leitura e escrita do arquivo .env, sem dependências externas.

O aplicativo carrega o .env automaticamente ao iniciar — ninguém precisa
exportar variáveis no terminal — e a tela de configuração grava aqui o que
for preenchido. Formato: KEY=valor, um por linha; comentários (#), linhas
vazias e o prefixo "export " são tolerados; aspas em volta do valor são
removidas. O arquivo é criado com permissão 0600 (só o dono lê).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE = ".env"


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):]
    key, _, value = line.partition("=")
    key, value = key.strip(), value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return (key, value) if key else None


def load_env_file(path: str | Path = DEFAULT_ENV_FILE, override: bool = False) -> dict[str, str]:
    """Carrega o arquivo para os.environ (variáveis já exportadas têm
    precedência, a menos que override=True) e devolve o que foi lido."""
    path = Path(path)
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        key, value = parsed
        values[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def save_env_file(path: str | Path, values: dict[str, str]) -> None:
    """Grava/atualiza as chaves informadas preservando comentários e as
    demais linhas do arquivo."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed and parsed[0] in remaining:
            out.append(f"{parsed[0]}={remaining.pop(parsed[0])}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
