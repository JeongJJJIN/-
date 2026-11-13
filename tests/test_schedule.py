"""정기 수집을 지금 돌릴 때인지: 예약이 늦거나 빠져도 다음 예약이 따라잡고, 이미 돈 회차는 다시 돌지 않는다."""

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teukbo import schedule

from .helpers import config

HOURS, MINUTE = [2, 5, 8, 11, 14, 17, 20, 23], 25
ROOT = Path(__file__).resolve().parent.parent
RETRY = timedelta(minutes=40)


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
        self.assertIn("ref: ${{ github.ref_name }}", workflow)  # 기다리는 동안 앞의 실행이 커밋한 이력까지 받고 시작한다(옛 커밋에서 시작하면 push 충돌)


def run(day: int, hour: int, minute: int, status: str = "completed") -> dict:
    return {"created_at": utc(day, hour, minute).strftime("%Y-%m-%dT%H:%M:%SZ"), "status": status}


class StartCollectTest(unittest.TestCase):
    """실시간 수집이 돌 때마다: 정기 계산이 밀렸으면 시작시키되, 같은 회차를 두 번 시작하지 않는다."""

    SLOT = utc(13, 8, 25)

    def test_starts_when_the_slot_passed_and_nothing_ran(self):
        earlier = [run(13, 5, 26), run(13, 7, 55)]  # 앞 회차의 실행, 때가 아니어서 건너뛴 예약
        start, why = schedule.should_start(utc(13, 8, 27), self.SLOT, utc(13, 5, 26), earlier, RETRY)
        self.assertTrue(start, why)
        self.assertTrue(schedule.should_start(utc(13, 8, 27), self.SLOT, None, [], RETRY)[0])  # 이력이 전혀 없을 때

    def test_does_not_start_twice_for_the_same_slot(self):
        last = utc(13, 5, 26)
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            start, why = schedule.should_start(utc(13, 8, 32), self.SLOT, last, [run(13, 8, 27, status)], RETRY)
            self.assertFalse(start, status)
            self.assertIn("이미 시작", why)
        self.assertFalse(schedule.should_start(utc(13, 8, 40), self.SLOT, utc(13, 8, 31), [], RETRY)[0])  # 이미 계산함

    def test_retries_a_failed_slot_only_after_the_gap(self):
        last, failed = utc(13, 5, 26), [run(13, 8, 27)]  # 끝났는데 이력이 없다 = 실패
        start, why = schedule.should_start(utc(13, 8, 50), self.SLOT, last, failed, RETRY)
        self.assertFalse(start)
        self.assertIn("40분", why)
        self.assertTrue(schedule.should_start(utc(13, 9, 7), self.SLOT, last, failed, RETRY)[0])
        self.assertFalse(schedule.should_start(utc(13, 9, 30), self.SLOT, last, [run(13, 9, 7), run(13, 8, 27)], RETRY)[0])  # 재시도도 간격을 지킨다

    def test_unreadable_run_records_are_ignored(self):
        self.assertTrue(schedule.should_start(utc(13, 8, 27), self.SLOT, None, [{"status": "queued"}, {"created_at": "?"}], RETRY)[0])

    def _start(self, now, last, runs, list_status=200, dispatch_status=200):
        calls = []

        def request(method, path, token, body=None):
            calls.append((method, path, token, body))
            if method == "GET":
                return list_status, {"workflow_runs": runs}
            return dispatch_status, None

        trigger = config().settings.raw["local"]["trigger"]
        return schedule.start_collect(trigger, now, last, "teukbo/teukbo.github.io", "tok", request), calls

    def test_sends_one_signal_that_asks_the_receiver_to_check_again(self):
        (sent, text), calls = self._start(utc(13, 8, 27), utc(13, 5, 26), [run(13, 5, 26)])
        self.assertTrue(sent, text)
        self.assertEqual([c[0] for c in calls], ["GET", "POST"])
        self.assertEqual(calls[1][1], "/repos/teukbo/teukbo.github.io/actions/workflows/collect.yml/dispatches")
        self.assertEqual(calls[1][3], {"ref": "main", "inputs": {"if_due": "true"}})

    def test_asks_github_nothing_when_not_due(self):
        (sent, _), calls = self._start(utc(13, 8, 40), utc(13, 8, 31), [])
        self.assertFalse(sent)
        self.assertEqual(calls, [])  # 대부분의 차례: API 호출 없이 끝난다

    def test_problems_never_raise(self):
        (sent, text), calls = self._start(utc(13, 8, 27), utc(13, 5, 26), [], list_status=None)
        self.assertFalse(sent)
        self.assertEqual(len(calls), 1)  # 기록을 못 읽었으면 신호도 보내지 않는다(두 번 시작 방지)
        (sent, text), _ = self._start(utc(13, 8, 27), utc(13, 5, 26), [], dispatch_status=403)
        self.assertFalse(sent)
        self.assertIn("403", text)

    def test_workflows_are_wired_for_the_signal(self):
        live = (ROOT / ".github" / "workflows" / "live.yml").read_text(encoding="utf-8")
        self.assertIn("actions: write", live)  # 신호를 보낼 권한
        self.assertIn("python3 -m teukbo.schedule --start-collect", live)
        self.assertIn("continue-on-error: true", live)  # 신호 쪽 문제로 실시간 수집이 실패로 남지 않게
        collect = (ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
        self.assertIn("if_due:", collect)
        self.assertIn('[ "${{ inputs.if_due }}" = "true" ]', collect)  # 받는 쪽이 한 번 더 확인한다

    def test_cloud_clock_script_matches_the_settings(self):
        """바깥 시계(구글 스크립트)는 저장소 밖에서 돌므로 값이 따로 적혀 있다 — 설정과 어긋나지 않게 한다."""
        script = (ROOT / "tools" / "cloud_clock.gs").read_text(encoding="utf-8")
        values = dict(re.findall(r"^var (REPO|WORKFLOW|REF) = '([^']+)';$", script, flags=re.M))
        trigger = config().settings.raw["local"]["trigger"]
        self.assertEqual(values, {"REPO": trigger["repository"], "WORKFLOW": trigger["live_workflow"], "REF": trigger["ref"]})
        self.assertNotRegex(script, r"github_pat_|ghp_")  # 토큰은 스크립트 속성에만 둔다


if __name__ == "__main__":
    unittest.main()
