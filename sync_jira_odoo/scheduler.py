"""Agendador interno: sincronização automática configurada pela tela.

A agenda vive em .sync_schedule.json (fora do git):

  {"enabled": true, "interval_minutes": 60, "daily_time": "",
   "last_started_utc": "..."}

- interval_minutes > 0  → roda a cada N minutos;
- daily_time "HH:MM"    → roda uma vez por dia nesse horário (de Brasília);
- last_started_utc      → protege contra disparo duplo e sobrevive a
  reinícios do aplicativo.

A thread do agendador verifica a agenda a cada poucos segundos e dispara a
sincronização incremental (o mesmo botão "Sincronizar agora", sem opções
extras) quando chega a hora — nunca em paralelo com uma execução manual.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("sync_jira_odoo")

DEFAULT_SCHEDULE_FILE = ".sync_schedule.json"
# Brasília não tem horário de verão atualmente; offset fixo é suficiente
TZ_BRASILIA = timezone(timedelta(hours=-3), "America/Sao_Paulo")
MIN_INTERVAL_MINUTES = 15


def load_schedule(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {"enabled": False, "interval_minutes": 0, "daily_time": "", "close_tasks": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"enabled": False, "interval_minutes": 0, "daily_time": "", "close_tasks": False}
    return {
        "enabled": bool(data.get("enabled")),
        "interval_minutes": int(data.get("interval_minutes") or 0),
        "daily_time": str(data.get("daily_time") or ""),
        "close_tasks": bool(data.get("close_tasks")),
        "last_started_utc": data.get("last_started_utc"),
    }


def save_schedule(path: str | Path, schedule: dict) -> None:
    Path(path).write_text(
        json.dumps(schedule, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def validate_schedule(data: dict) -> tuple[dict | None, str]:
    """Normaliza o que veio da tela; devolve (agenda, "") ou (None, erro)."""
    enabled = bool(data.get("enabled"))
    interval = int(data.get("interval_minutes") or 0)
    daily = str(data.get("daily_time") or "").strip()
    if interval and daily:
        return None, "escolha intervalo OU horário diário, não os dois"
    if enabled and not interval and not daily:
        return None, "escolha a frequência da sincronização automática"
    if interval and interval < MIN_INTERVAL_MINUTES:
        return None, f"intervalo mínimo: {MIN_INTERVAL_MINUTES} minutos"
    if daily:
        try:
            hh, mm = daily.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
        except ValueError:
            return None, "horário inválido (use HH:MM)"
    return {
        "enabled": enabled,
        "interval_minutes": interval,
        "daily_time": daily,
        "close_tasks": bool(data.get("close_tasks")),
    }, ""


def _parse_last(schedule: dict) -> datetime | None:
    raw = schedule.get("last_started_utc")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _daily_target(schedule: dict, now_utc: datetime) -> datetime:
    """O horário-alvo de hoje (em UTC) para agendas diárias."""
    hh, mm = (int(part) for part in schedule["daily_time"].split(":"))
    now_local = now_utc.astimezone(TZ_BRASILIA)
    return now_local.replace(hour=hh, minute=mm, second=0, microsecond=0).astimezone(timezone.utc)


def is_due(schedule: dict, now_utc: datetime) -> bool:
    if not schedule.get("enabled"):
        return False
    last = _parse_last(schedule)
    interval = int(schedule.get("interval_minutes") or 0)
    if interval > 0:
        return last is None or (now_utc - last) >= timedelta(minutes=interval)
    if schedule.get("daily_time"):
        target = _daily_target(schedule, now_utc)
        return now_utc >= target and (last is None or last < target)
    return False


def next_due(schedule: dict, now_utc: datetime) -> datetime | None:
    """Próximo disparo previsto (UTC), para exibição na tela."""
    if not schedule.get("enabled"):
        return None
    last = _parse_last(schedule)
    interval = int(schedule.get("interval_minutes") or 0)
    if interval > 0:
        if last is None:
            return now_utc
        return max(now_utc, last + timedelta(minutes=interval))
    if schedule.get("daily_time"):
        target = _daily_target(schedule, now_utc)
        if now_utc >= target and not (last is None or last < target):
            target += timedelta(days=1)
        return max(now_utc, target) if target >= now_utc else target
    return None


class Scheduler(threading.Thread):
    """Thread de fundo que dispara runner.start() quando a agenda manda."""

    def __init__(self, runner, path: str | Path, poll_seconds: float = 20.0):
        super().__init__(daemon=True, name="sync-scheduler")
        self.runner = runner
        self.path = Path(path)
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as exc:  # o agendador nunca pode morrer
                log.error("agendador: %s", exc)

    def tick(self, now_utc: datetime | None = None) -> bool:
        """Uma verificação; devolve True se disparou uma sincronização."""
        now_utc = now_utc or datetime.now(timezone.utc)
        schedule = load_schedule(self.path)
        if not is_due(schedule, now_utc) or self.runner.running:
            return False
        if not self.runner.start(
            dry_run=False,
            since=None,
            delete=False,
            scheduled=True,
            close_tasks=bool(schedule.get("close_tasks")),
        ):
            return False
        schedule["last_started_utc"] = now_utc.isoformat()
        save_schedule(self.path, schedule)
        log.info("sincronização automática disparada pelo agendador")
        return True
