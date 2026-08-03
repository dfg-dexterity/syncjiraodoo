from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sync_jira_odoo.scheduler import (
    Scheduler,
    is_due,
    load_schedule,
    next_due,
    save_schedule,
    validate_schedule,
)

NOW = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)  # 12:00 em Brasília


def sched(**kwargs) -> dict:
    base = {"enabled": True, "interval_minutes": 0, "daily_time": ""}
    base.update(kwargs)
    return base


class IsDueTest(unittest.TestCase):
    def test_disabled_never_fires(self):
        self.assertFalse(is_due(sched(enabled=False, interval_minutes=30), NOW))

    def test_interval_fires_first_time_and_after_elapsed(self):
        agenda = sched(interval_minutes=60)
        self.assertTrue(is_due(agenda, NOW))
        agenda["last_started_utc"] = (NOW - timedelta(minutes=30)).isoformat()
        self.assertFalse(is_due(agenda, NOW))
        agenda["last_started_utc"] = (NOW - timedelta(minutes=61)).isoformat()
        self.assertTrue(is_due(agenda, NOW))

    def test_daily_fires_once_after_target_time(self):
        agenda = sched(daily_time="11:00")  # 11:00 Brasília = 14:00 UTC
        self.assertTrue(is_due(agenda, NOW))  # 12:00 local, já passou das 11:00
        agenda["last_started_utc"] = (NOW - timedelta(minutes=30)).isoformat()
        self.assertFalse(is_due(agenda, NOW))  # já rodou hoje depois do alvo
        agenda["last_started_utc"] = (NOW - timedelta(days=1)).isoformat()
        self.assertTrue(is_due(agenda, NOW))  # último disparo foi ontem

    def test_daily_waits_for_target_time(self):
        agenda = sched(daily_time="20:00")  # ainda não deu 20:00 em Brasília
        self.assertFalse(is_due(agenda, NOW))

    def test_next_due_reporting(self):
        self.assertIsNone(next_due(sched(enabled=False), NOW))
        agenda = sched(interval_minutes=60,
                       last_started_utc=(NOW - timedelta(minutes=20)).isoformat())
        self.assertEqual(next_due(agenda, NOW), NOW + timedelta(minutes=40))


class ValidateTest(unittest.TestCase):
    def test_requires_frequency_when_enabled(self):
        schedule, error = validate_schedule({"enabled": True})
        self.assertIsNone(schedule)
        self.assertIn("frequência", error)

    def test_rejects_both_modes_and_bad_values(self):
        self.assertIsNone(validate_schedule(
            {"enabled": True, "interval_minutes": 60, "daily_time": "06:00"})[0])
        self.assertIsNone(validate_schedule({"enabled": True, "interval_minutes": 5})[0])
        self.assertIsNone(validate_schedule({"enabled": True, "daily_time": "25:99"})[0])

    def test_accepts_valid_modes(self):
        schedule, error = validate_schedule({"enabled": True, "interval_minutes": 30})
        self.assertEqual(error, "")
        self.assertEqual(schedule["interval_minutes"], 30)
        schedule, error = validate_schedule({"enabled": True, "daily_time": "06:30"})
        self.assertEqual(schedule["daily_time"], "06:30")


class FakeRunner:
    def __init__(self):
        self.running = False
        self.calls = []

    def start(self, dry_run, since, delete, scheduled=False, close_tasks=False):
        self.calls.append({
            "dry_run": dry_run, "since": since,
            "scheduled": scheduled, "close_tasks": close_tasks,
        })
        return True


class SchedulerTickTest(unittest.TestCase):
    def test_tick_fires_and_records_last_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.json"
            save_schedule(path, sched(interval_minutes=30))
            runner = FakeRunner()
            scheduler = Scheduler(runner, path)
            self.assertTrue(scheduler.tick(NOW))
            self.assertEqual(runner.calls[0], {
                "dry_run": False, "since": None,
                "scheduled": True, "close_tasks": False,
            })
            # o disparo ficou registrado: o tick seguinte não duplica
            self.assertFalse(scheduler.tick(NOW + timedelta(minutes=5)))
            self.assertTrue(scheduler.tick(NOW + timedelta(minutes=31)))

    def test_tick_skips_while_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.json"
            save_schedule(path, sched(interval_minutes=30))
            runner = FakeRunner()
            runner.running = True
            self.assertFalse(Scheduler(runner, path).tick(NOW))
            self.assertEqual(runner.calls, [])

    def test_load_handles_missing_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.json"
            self.assertFalse(load_schedule(path)["enabled"])
            path.write_text("{corrompido", encoding="utf-8")
            self.assertFalse(load_schedule(path)["enabled"])


if __name__ == "__main__":
    unittest.main()
