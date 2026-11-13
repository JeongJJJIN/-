"""정기 수집을 지금 돌릴 때인지: 예약이 늦거나 빠져도 다음 예약이 따라잡고, 이미 돈 회차는 다시 돌지 않는다."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from teukbo import schedule

from .helpers import config

HOURS, MINUTE = [2, 5, 8, 11, 14, 17, 20, 23], 25


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


class SlotTest(unittest.TestCase):
    def test_latest_slot_is_the_most_recent_scheduled_time(self):
        self.assertEqual(schedule.latest_slot(utc(13, 8, 25), HOURS, MINUTE), utc(13, 8, 25))  # 정각에 온 예약
        self.assertEqual(schedule.latest_slot(utc(13, 8, 55), HOURS, MINUTE), utc(13, 8, 25))
        self.assertEqual(schedule.latest_slot(utc(13, 8, 24), HOURS, MINUTE), utc(13, 5, 25))
        self.assertEqual(schedule.latest_slot(utc(13, 0, 10), HOURS, MINUTE), utc(12, 23, 25))  # 자정을 넘긴 직후

    def test_due_only_until_the_slot_has_been_run(self):
        last = utc(13, 5, 26)  # 14:26 KST 에 돈 것이 마지막
        self.assertFalse(schedule.is_due(utc(13, 7, 55), last, HOURS, MINUTE))  # 다음 정기 시각 전: 건너뜀
        self.assertTrue(schedule.is_due(utc(13, 8, 25), last, HOURS, MINUTE))  # 정기 시각: 돈다
        self.assertTrue(schedule.is_due(utc(13, 9, 55), last, HOURS, MINUTE))  # 예약이 빠졌어도 뒤의 예약이 따라잡는다
        self.assertFalse(schedule.is_due(utc(13, 8, 55), utc(13, 8, 31), HOURS, MINUTE))  # 이미 돈 회차는 다시 돌지 않는다
        self.assertTrue(schedule.is_due(utc(13, 8, 25), None, HOURS, MINUTE))  # 이력이 없으면 돈다

    def test_last_run_comes_from_the_history_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp)
            self.assertIsNone(schedule.last_run_time(history))
            (history / "2026-11-12.json").write_text(json.dumps({"runs": [{"t": "2026-11-12T23:30+09:00"}]}), encoding="utf-8")
            (history / "2026-11-13.json").write_text(json.dumps({"runs": [{"t": "2026-11-13T11:39+09:00"}, {"t": "2026-11-13T14:26+09:00"}, {"bad": 1}]}), encoding="utf-8")
            self.assertEqual(schedule.last_run_time(history), utc(13, 5, 26))

    def test_slots_match_the_settings_and_the_workflow(self):
        """정해진 시각은 설정 파일에 있고, 워크플로는 30분마다 확인만 한다."""
        trigger = config().settings.raw["local"]["trigger"]
        self.assertEqual((trigger["collect_utc_hours"], trigger["collect_minute"]), (HOURS, MINUTE))
        workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "25,55 * * * *"', workflow)
        self.assertIn("python3 -m teukbo.schedule", workflow)
        self.assertIn("needs.collect.outputs.ran == 'true'", workflow)  # 건너뛴 회차는 배포도 하지 않는다


if __name__ == "__main__":
    unittest.main()
