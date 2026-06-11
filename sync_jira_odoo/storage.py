"""Estado incremental e histórico de execuções (arquivos locais, fora do git)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_STATE_FILE = ".sync_state.json"
DEFAULT_HISTORY_FILE = ".sync_history.json"
DEFAULT_LOOKBACK_DAYS = 7
HISTORY_KEEP = 200


def load_since(explicit: str | None, state_path: Path) -> datetime:
    """Prioridade: valor explícito > última execução > 7 dias atrás."""
    if explicit:
        dt = datetime.fromisoformat(explicit)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if state_path.exists():
        state = json.loads(state_path.read_text())
        return datetime.fromisoformat(state["last_sync_utc"])
    return datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def save_state(state_path: Path, run_started: datetime) -> None:
    state_path.write_text(json.dumps({"last_sync_utc": run_started.isoformat()}, indent=2))


def append_history(path: Path, record: dict, keep: int = HISTORY_KEEP) -> None:
    history = read_history(path)
    history.append(record)
    path.write_text(json.dumps(history[-keep:], indent=2, ensure_ascii=False))


def read_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))
