"""내 컴퓨터를 서버로 쓰기: 수집 → 계산 → 이 컴퓨터(원하면 같은 네트워크의 다른 기기)의 브라우저로 보기.

윈도우는 start.bat, 맥은 start.command 를 두 번 누르면 이 모듈이 실행된다. 창을 열어 둔 동안:

- 실시간 자료(특보·예비특보, 지금 날씨, 앞으로 6시간, 선로 강우)를 2분마다 다시 받는다.
- 특보 가능성 예측은 3시간마다 다시 계산하고, 열차 운행 기록(전날까지)도 그때 채운다.
- (선택) local_keys.txt 에 GITHUB_TRIGGER_TOKEN 이 있으면 공개 사이트가 제때 갱신되도록 GitHub 에 '실행' 신호를 보낸다.
  GitHub 의 예약 실행이 늦거나 돌지 않을 때 이 컴퓨터가 시계 노릇을 하는 것이다(자료는 GitHub 이 직접 받는다).

    python -m teukbo.local            계속 실행 (창을 닫으면 멈춤)
    python -m teukbo.local --share    같은 네트워크의 다른 기기(휴대폰·다른 PC)에서도 볼 수 있게 연다
    python -m teukbo.local --once     한 번만 수집
    python -m teukbo.local --demo     인터넷·인증키 없이 가상 자료로 화면만 보기

- 화면은 기본으로 이 컴퓨터에서만 열린다(http://127.0.0.1:포트). --share 를 주면 같은 네트워크에서도 열린다.
- 인증키는 처음 한 번 물어보고 local_keys.txt 에 저장한다(이 컴퓨터에만 남고 저장소에는 올라가지 않는다).
- 이력은 data/history 에 쌓인다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import live as live_mod
from . import rail as rail_mod
from . import rail_run
from . import run as run_mod
from . import storage
from .config import ROOT, Config, load_config
from .timeutil import now_kst

KEY_FILE = ROOT / "local_keys.txt"
TRIGGER_TOKEN = "GITHUB_TRIGGER_TOKEN"  # (선택) 공개 사이트 갱신 신호용 GitHub 토큰 — 이 저장소의 Actions 읽기·쓰기 권한만
KEY_NAMES = ("DATA_GO_KR_SERVICE_KEY", "KMA_APIHUB_KEY", TRIGGER_TOKEN)
INTERVAL_HOURS = 3
RETRY_MINUTES = 30
LIVE_STATE = "live_state.json"  # 레이더 최근 1시간 값(다음 실시간 수집이 이어 쓴다). 화면에는 내보내지 않는다.
GITHUB_API = "https://api.github.com"
PORTS = range(8000, 8011)
GRADE_KO = {"caution": "주의", "high": "높음", "active": "발효 중"}
# 화면이 읽는 자료만 내보낸다(이력 등 다른 파일은 제외)
_DATA_FILES = {"latest.json", "accuracy.json", "warnings.json", "live.json"}


# ---------------------------------------------------------------- 인증키


def read_keys(path: Path) -> dict[str, str]:
    """local_keys.txt (이름=값 한 줄씩)를 읽는다. 없으면 빈 사전."""
    keys: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return keys
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() in KEY_NAMES:
            keys[name.strip()] = value.strip()
    return keys


def write_keys(path: Path, keys: dict[str, str]) -> None:
    lines = [
        "# 기상특보 가능성 - 이 컴퓨터에서만 쓰는 인증키 (다른 사람에게 보내지 마세요)",
        "# 바꾸려면 = 뒤의 값을 고치거나, 이 파일을 지우고 다시 실행하세요.",
        f"# {TRIGGER_TOKEN}: (선택) 공개 사이트를 이 컴퓨터가 제때 갱신하게 하려면 GitHub 토큰을 넣으세요. 안 쓰면 비워 둡니다.",
    ]
    lines += [f"{name}={keys.get(name, '')}" for name in KEY_NAMES]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ask_keys() -> dict[str, str]:
    print()
    print("처음 실행입니다. 공공데이터포털 인증키를 한 번만 입력하면 이 컴퓨터에 저장해 둡니다.")
    print("  (data.go.kr 로그인 > 마이페이지 > 인증키 발급현황 의 '일반 인증키')")
    print("  인증키가 없으면 그냥 Enter 를 누르세요. 그러면 기상청 특보·단기예보 없이 해외 수치모델만으로 계산합니다.")
    print()
    data_key = input("공공데이터포털 인증키 붙여넣기 후 Enter: ").strip()
    return {"DATA_GO_KR_SERVICE_KEY": data_key, "KMA_APIHUB_KEY": "", TRIGGER_TOKEN: ""}


# ---------------------------------------------------------------- 이 컴퓨터에서만 여는 웹 서버


def resolve_path(url_path: str, web_dir: Path, data_dir: Path) -> Path | None:
    """요청 주소 → 실제 파일. 화면 파일은 web/, 자료는 data/ 의 정해진 파일만. 그 밖은 None."""
    clean = urllib.parse.unquote(urllib.parse.urlsplit(url_path).path)
    parts = [p for p in clean.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." or ":" in p for p in parts):
        return None
    if parts[:1] == ["data"]:
        rest = parts[1:]
        if len(rest) == 1 and rest[0] in _DATA_FILES:
            return data_dir / rest[0]
        if rest == ["rail.json"]:  # 열차 지연 통계 (배포 때는 이 이름으로 복사된다)
            return data_dir / "rail" / "summary.json"
        if rest == ["rail-cube.json"]:
            return data_dir / "rail" / "cube.json"
        if len(rest) == 2 and rest[0] == "rail-days" and rest[1].endswith(".json"):
            return data_dir / "rail" / "days" / rest[1]
        if len(rest) == 2 and rest[0] == "detail" and rest[1].endswith(".json"):
            return data_dir / "detail" / rest[1]
        return None
    return web_dir.joinpath(*parts) if parts else web_dir / "index.html"


def make_handler(web_dir: Path, data_dir: Path) -> type[SimpleHTTPRequestHandler]:
    class Handler(SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            target = resolve_path(path, web_dir, data_dir)
            return str(target if target is not None else web_dir / "__없는_파일__")

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")  # 항상 방금 수집한 자료를 보여 준다
            super().end_headers()

        def log_message(self, *args: object) -> None:  # 요청 기록은 화면에 찍지 않는다
            pass

    return Handler


def start_server(web_dir: Path, data_dir: Path, ports: range = PORTS, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, int]:
    """서버를 띄운다. 기본은 127.0.0.1(이 컴퓨터에서만). host 가 0.0.0.0 이면 같은 네트워크의 다른 기기에서도 열린다.
    어느 쪽이든 내보내는 것은 화면 파일과 정해진 자료 파일뿐이다(resolve_path)."""
    handler = make_handler(web_dir, data_dir)
    last_error: OSError | None = None
    for port in ports:
        try:
            server = ThreadingHTTPServer((host, port), handler)
        except OSError as e:
            last_error = e
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, port
    raise OSError(f"사용할 수 있는 포트가 없습니다 ({ports.start}~{ports.stop - 1}): {last_error}")


def lan_address() -> str | None:
    """같은 네트워크의 다른 기기가 접속할 이 컴퓨터의 주소. 아무것도 보내지 않고, 바깥으로 나가는 쪽 주소만 알아낸다."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return str(s.getsockname()[0])
    except OSError:
        return None


# ---------------------------------------------------------------- 실시간 자료 (몇 분마다)


def _read_quiet(path: Path) -> Any:
    try:
        return storage.read_json(path)
    except ValueError:
        return None


def _write_retry(path: Path, data: Any, tries: int = 4) -> None:
    """윈도우에서는 화면이 그 파일을 읽는 순간에 바꿔치기가 거절될 수 있어, 잠깐 쉬었다가 다시 쓴다."""
    for attempt in range(tries):
        try:
            storage.write_json(path, data)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.3)


def live_once(cfg: Config, data_dir: Path, env: Mapping[str, str], now: datetime | None = None) -> dict[str, Any]:
    """실시간 자료 한 번: 특보·지금 날씨·앞으로 6시간·선로 강우 → data/live.json (GitHub 의 live.yml 과 같은 계산)."""
    now = now or now_kst().replace(second=0, microsecond=0)
    prev = _read_quiet(data_dir / "live.json")
    doc, state = live_mod.collect_all(cfg, now, env, prev, _read_quiet(data_dir / LIVE_STATE))
    if state is not None:
        _write_retry(data_dir / LIVE_STATE, state)
    _write_retry(data_dir / "live.json", doc)
    return doc


def live_signature(doc: Mapping[str, Any]) -> str:
    """달라지면 창에 한 줄 알릴 만한 것: 특보·예비특보 목록, 각 자료를 받았는지, 비가 오는 노선."""
    warnings = doc.get("warnings") or {}
    rain = doc.get("rail_rain") or {}
    wet = sorted(str(line.get("name")) for line in rain.get("lines", []) if (line.get("now") or 0) > 0)
    parts = [warnings.get("available"), warnings.get("active"), warnings.get("preliminary"), (doc.get("obs") or {}).get("available"), (doc.get("fcst") or {}).get("available"), rain.get("available"), wet]
    return json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)


def live_loop(cfg: Config, data_dir: Path, env: Mapping[str, str], stop: threading.Event, interval: float) -> None:
    """창을 열어 둔 동안 interval 초마다 실시간 자료를 다시 받는다. 달라진 것이 있을 때만 한 줄 적는다."""
    last_sig: str | None = None
    last_error: str | None = None
    while not stop.is_set():
        started = time.monotonic()
        stamp = f"[{now_kst():%m-%d %H:%M}]"
        try:
            doc = live_once(cfg, data_dir, env)
        except Exception as e:  # noqa: BLE001 - 한 번 실패해도 계속 돈다
            note = f"{type(e).__name__}: {e}"
            if note != last_error:
                print(f"{stamp} 실시간 자료를 받지 못했습니다(계속 다시 시도합니다): {note}", flush=True)
            last_error = note
        else:
            sig = live_signature(doc)
            if sig != last_sig or last_error is not None:
                print(f"{stamp} 실시간: {live_mod.summary_line(doc)}", flush=True)
            last_sig, last_error = sig, None
        stop.wait(max(5.0, interval - (time.monotonic() - started)))


def use_local_live(data_dir: Path, cfg: Config) -> None:
    """화면이 실시간 자료를 이 컴퓨터가 방금 받은 파일에서, 더 자주 읽도록 latest.json 에 적어 둔다."""
    path = data_dir / "latest.json"
    latest = _read_quiet(path)
    if not isinstance(latest, dict):
        return
    raw = cfg.settings.raw
    wanted = {"url": "data/live.json", "stale_after_minutes": raw.get("live", {}).get("stale_after_minutes", 30), "poll_seconds": raw.get("local", {}).get("poll_seconds", 60)}
    if latest.get("live") != wanted:
        latest["live"] = wanted
        _write_retry(path, latest)


def ensure_cube(data_dir: Path) -> None:
    """분석 화면이 쓰는 묶음 집계가 없으면, 쌓여 있는 날짜별 운행 기록으로 만든다(인증키 없이도 분석 화면이 열리게)."""
    days_dir, cube = data_dir / "rail" / "days", data_dir / "rail" / "cube.json"
    if not cube.is_file() and days_dir.is_dir() and any(days_dir.glob("*.json")):
        storage.write_json(cube, rail_mod.build_cube(rail_run.load_days(data_dir)))


def rail_once(cfg: Config, data_dir: Path, env: Mapping[str, str]) -> None:
    """열차 운행 기록(전날까지)을 채우고 분석용 집계를 다시 만든다. 인증키가 없으면 아무것도 하지 않는다."""
    if not (env.get("DATA_GO_KR_SERVICE_KEY") or "").strip():
        return
    print("  열차 운행 기록을 확인하는 중입니다… (처음에는 몇 분 걸릴 수 있습니다)", flush=True)
    try:
        rail_run.run(cfg, now_kst().replace(second=0, microsecond=0), data_dir, env)
    except Exception as e:  # noqa: BLE001
        print(f"  열차 운행 기록: 실패 ({type(e).__name__}: {e})")
        return
    period = (_read_quiet(data_dir / "rail" / "summary.json") or {}).get("period") or {}
    print(f"  열차 운행 기록: {period['from']} ~ {period['to']} ({period['days']}일)" if period.get("days") else "  열차 운행 기록: 아직 없습니다")


# ---------------------------------------------------------------- 공개 사이트 갱신 신호 (선택)


def github_request(method: str, path: str, token: str, body: Any = None, timeout: float = 20.0) -> tuple[int | None, Any]:
    """GitHub API 한 번. 반환: (상태 코드 또는 None(연결 실패), 응답 JSON). 토큰은 머리글로만 보내고 어디에도 찍지 않는다."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(GITHUB_API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 주소는 위의 고정 값
            status, text = response.status, response.read()
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, {"message": type(e).__name__}
    try:
        return status, (json.loads(text) if text else None)
    except ValueError:
        return status, None


class TriggerError(RuntimeError):
    def __init__(self, status: int | None, where: str):
        super().__init__(f"{where}: {status}")
        self.status, self.where = status, where

    def explain(self, repo: str) -> str:
        if self.status is None:
            return "공개 사이트 갱신 신호: GitHub 에 연결하지 못했습니다(인터넷 확인). 계속 다시 시도합니다."
        if self.status == 401:
            return f"공개 사이트 갱신 신호: GitHub 토큰이 맞지 않거나 기한이 지났습니다. local_keys.txt 의 {TRIGGER_TOKEN} 을 새로 넣어 주세요."
        if self.status == 403:
            return "공개 사이트 갱신 신호: 토큰에 권한이 없습니다. 토큰을 만들 때 이 저장소의 'Actions: Read and write' 를 주었는지 확인해 주세요."
        if self.status == 404:
            return f"공개 사이트 갱신 신호: 저장소({repo})나 실행 파일을 찾지 못했습니다. 토큰이 이 저장소를 고르고 만든 것인지 확인해 주세요."
        return f"공개 사이트 갱신 신호: GitHub 이 요청을 받지 않았습니다(코드 {self.status}, {self.where}). 계속 다시 시도합니다."


class Trigger:
    """GitHub 의 예약 실행 대신 제때 '실행' 신호(workflow_dispatch)를 보낸다. 자료는 GitHub 이 받고, 이 컴퓨터는 시계 노릇만 한다.

    GitHub 의 예약 실행이 제대로 돌고 있으면(최근 실행 기록이 있으면) 신호를 보내지 않는다 — 두 번 돌리지 않으려고."""

    def __init__(self, settings: Mapping[str, Any], token: str, request: Callable[..., tuple[int | None, Any]] | None = None):
        self.repo = str(settings.get("repository") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError(f"config/settings.json 의 local.trigger.repository 가 '소유자/이름' 꼴이 아닙니다: {self.repo!r}")
        self.ref = str(settings.get("ref", "main"))
        self.live_workflow = str(settings.get("live_workflow", "live.yml"))
        self.live_every = timedelta(minutes=float(settings.get("live_every_minutes", 5)))
        self.collect_workflow = str(settings.get("collect_workflow", "collect.yml"))
        self.collect_hours = sorted({int(h) for h in settings.get("collect_utc_hours", [])})
        self.collect_minute = int(settings.get("collect_minute", 25))
        self.token = token
        self.request = request or github_request
        self.seen: dict[str, datetime] = {}  # 실행 파일별: 마지막으로 돈 시각(GitHub 기록) 또는 우리가 신호를 보낸 시각
        self.last_error: str | None = None
        self.announced = False

    def collect_slot(self, now: datetime) -> datetime | None:
        """now(UTC) 이전의 가장 가까운 '수집·계산·배포' 예정 시각."""
        for back in (0, 1):
            day = (now - timedelta(days=back)).date()
            for hour in reversed(self.collect_hours):
                slot = datetime(day.year, day.month, day.day, hour, self.collect_minute, tzinfo=timezone.utc)
                if slot <= now:
                    return slot
        return None

    def last_run(self, workflow: str) -> datetime | None:
        status, body = self.request("GET", f"/repos/{self.repo}/actions/workflows/{workflow}/runs?per_page=1", self.token)
        if status != 200:
            raise TriggerError(status, f"{workflow} 실행 기록 조회")
        runs = (body or {}).get("workflow_runs") or []
        return datetime.fromisoformat(str(runs[0]["created_at"]).replace("Z", "+00:00")) if runs else None

    def dispatch(self, workflow: str) -> None:
        status, _ = self.request("POST", f"/repos/{self.repo}/actions/workflows/{workflow}/dispatches", self.token, {"ref": self.ref})
        if status not in (200, 201, 204):
            raise TriggerError(status, f"{workflow} 실행 신호")

    def _due(self, workflow: str, threshold: datetime) -> bool:
        """threshold 뒤로 돈 적이 없으면 True. 기억해 둔 시각으로 먼저 보고, 때가 된 것 같으면 GitHub 기록을 확인한다(예약 실행이 돌았을 수 있다)."""
        seen = self.seen.get(workflow)
        if seen is not None and seen >= threshold:
            return False
        last = self.last_run(workflow)
        if last is not None and (seen is None or last > seen):
            self.seen[workflow] = seen = last
        return seen is None or seen < threshold

    def tick(self, now: datetime) -> list[str]:
        """할 일이 있으면 신호를 보낸다. 반환: 창에 적을 줄(대개 빈 목록)."""
        lines: list[str] = []
        try:
            if self._due(self.live_workflow, now - self.live_every + timedelta(seconds=30)):
                self.dispatch(self.live_workflow)
                self.seen[self.live_workflow] = now
                if not self.announced:
                    lines.append(f"공개 사이트 갱신 신호: 보내기 시작했습니다({self.repo}, 실시간 {self.live_every.total_seconds() / 60:g}분마다).")
                    self.announced = True
            slot = self.collect_slot(now)
            if slot is not None and self._due(self.collect_workflow, slot):
                self.dispatch(self.collect_workflow)
                self.seen[self.collect_workflow] = now
                lines.append("공개 사이트 갱신 신호: 3시간 주기의 수집·계산·배포를 시작시켰습니다.")
            if self.last_error is not None:
                lines.append("공개 사이트 갱신 신호: 다시 정상입니다.")
                self.last_error = None
        except TriggerError as e:
            text = e.explain(self.repo)
            if text != self.last_error:
                lines.append(text)
            self.last_error = text
        return lines


def trigger_loop(trigger: Trigger, stop: threading.Event, every: float = 30.0) -> None:
    while not stop.is_set():
        try:
            for line in trigger.tick(datetime.now(timezone.utc)):
                print(f"[{now_kst():%m-%d %H:%M}] {line}", flush=True)
        except Exception as e:  # noqa: BLE001 - 신호 쪽 문제로 수집을 멈추지 않는다
            print(f"[{now_kst():%m-%d %H:%M}] 공개 사이트 갱신 신호: 예상치 못한 오류({type(e).__name__}). 계속 다시 시도합니다.", flush=True)
        stop.wait(every)


# ---------------------------------------------------------------- 실행


def summarize(data_dir: Path) -> list[str]:
    """방금 결과에서 '주의' 이상인 지역을 한 줄씩."""
    latest = storage.read_json(data_dir / "latest.json") or {}
    names = {h["id"]: h["name"] for h in latest.get("hazards", [])}
    lines = []
    for region in latest.get("regions", []):
        top = region.get("top") or {}
        if top.get("grade") in GRADE_KO:
            pct = f" {top['p']}%" if top.get("p") is not None and top["grade"] != "active" else ""
            lines.append(f"  {region['name']}: {names.get(top.get('hazard'), '')} {GRADE_KO[top['grade']]}{pct}")
    return lines


def collect_once(cfg: Config, data_dir: Path, env: dict[str, str], demo: bool) -> bool:
    now = now_kst().replace(second=0, microsecond=0)
    print(f"[{now:%m-%d %H:%M}] 자료를 받는 중입니다…", flush=True)
    try:
        code = run_mod.run_demo(cfg, now, data_dir, days=1) if demo else run_mod.run_real(cfg, now, data_dir, env)
    except Exception as e:  # noqa: BLE001 - 한 번 실패해도 창은 살려 둔다
        print(f"  실패: {type(e).__name__}: {e}")
        return False
    if code != run_mod.EXIT_OK:
        print("  예측 자료를 받지 못했습니다. 인터넷 연결을 확인해 주세요.")
        return False
    latest = storage.read_json(data_dir / "latest.json") or {}
    for name, label in (("kma_warnings", "기상청 특보"), ("kma_forecast", "기상청 단기예보"), ("open_meteo", "해외 수치모델")):
        info = (latest.get("sources") or {}).get(name) or {}
        state = "정상" if info.get("ok") else ("인증키 없음" if info.get("configured") is False else f"실패 ({info.get('error')})")
        print(f"  {label}: {state}")
    notable = summarize(data_dir)
    print("  '주의' 이상 지역:" if notable else "  '주의' 이상인 지역이 없습니다.")
    for line in notable:
        print(line)
    return True


def wait_until(deadline: datetime) -> None:
    while now_kst() < deadline:
        time.sleep(min(30.0, max(0.5, (deadline - now_kst()).total_seconds())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="기상특보 가능성 - 내 컴퓨터를 서버로 쓰기")
    parser.add_argument("--once", action="store_true", help="한 번만 수집하고 화면을 띄운 채로 둔다")
    parser.add_argument("--share", action="store_true", help="같은 네트워크의 다른 기기(휴대폰·다른 PC)에서도 화면을 볼 수 있게 연다")
    parser.add_argument("--demo", action="store_true", help="인터넷·인증키 없이 가상 자료로 화면만 본다")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않는다")
    parser.add_argument("--exit-after-collect", action="store_true", help=argparse.SUPPRESS)  # 시험용
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):  # 일부 윈도우 콘솔에서 표시 못 하는 글자가 있어도 멈추지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.ERROR, format="  (%(name)s) %(message)s", stream=sys.stderr)
    logging.getLogger("teukbo.http").setLevel(logging.CRITICAL)  # 재시도 과정은 생략하고 최종 결과만 보여 준다

    print("기상특보 가능성 - 내 컴퓨터를 서버로")
    print("참고용입니다. 공식 특보·예비특보가 우선합니다.")
    if args.demo:
        print("※ 데모 모드: 아래 수치는 실제 예보가 아닌 가상 자료입니다.")
    cfg = load_config()
    data_dir = ROOT / ("data-demo" if args.demo else "data")
    env = dict(os.environ)
    token = (env.pop(TRIGGER_TOKEN, "") or "").strip()  # 갱신 신호용 토큰은 수집 쪽에 넘기지 않는다
    if not args.demo:
        keys = read_keys(KEY_FILE)
        if not KEY_FILE.exists() and not env.get("DATA_GO_KR_SERVICE_KEY") and sys.stdin and sys.stdin.isatty():
            keys = ask_keys()
            write_keys(KEY_FILE, keys)
            print(f"저장했습니다: {KEY_FILE.name} (바꾸려면 이 파일을 고치거나 지우세요)")
        token = token or keys.pop(TRIGGER_TOKEN, "").strip()
        for name, value in keys.items():
            if value and not env.get(name):
                env[name] = value

    raw = cfg.settings.raw.get("local", {})
    stop = threading.Event()
    realtime = not args.demo and not args.once and not args.exit_after_collect
    if not args.demo:
        env["TEUKBO_LIVE_URL"] = "data/live.json"  # 화면이 실시간 자료를 이 컴퓨터에서 읽게 한다
        try:
            ensure_cube(data_dir)
        except Exception as e:  # noqa: BLE001 - 분석 화면만 비고 나머지는 그대로 쓴다
            print(f"  분석용 집계를 만들지 못했습니다: {type(e).__name__}: {e}")
    if realtime:
        interval = float(raw.get("live_interval_seconds", 120))
        print(f"실시간 자료(특보·지금 날씨·6시간 예보·선로 강우): {interval / 60:g}분마다 다시 받습니다. 예측 계산은 {INTERVAL_HOURS}시간마다.")
        threading.Thread(target=live_loop, args=(cfg, data_dir, env, stop, interval), daemon=True).start()
        if token:
            try:
                trigger = Trigger(raw.get("trigger", {}), token)
            except ValueError as e:
                print(f"공개 사이트 갱신 신호: 꺼짐 — {e}")
            else:
                print(f"공개 사이트 갱신 신호: 켜짐 ({trigger.repo}) — 이 창이 열려 있는 동안 공개 사이트도 제때 갱신됩니다.")
                threading.Thread(target=trigger_loop, args=(trigger, stop), daemon=True).start()
        else:
            print(f"공개 사이트 갱신 신호: 꺼짐 (이 컴퓨터 화면만 실시간). 켜려면 local_keys.txt 에 {TRIGGER_TOKEN} 을 넣으세요 — README 참고.")
    elif not args.demo:
        try:
            live_once(cfg, data_dir, env)  # 한 번만 수집 모드에서도 지금 특보·날씨는 싣는다
        except Exception as e:  # noqa: BLE001
            print(f"  실시간 자료를 받지 못했습니다: {type(e).__name__}: {e}")

    server = None
    try:
        while True:
            ok = collect_once(cfg, data_dir, env, args.demo)
            if not args.demo:
                use_local_live(data_dir, cfg)  # 이번 수집이 실패해 지난 결과를 보여 주는 때에도, 실시간 자료는 이 컴퓨터 것을 쓴다
            if args.exit_after_collect:
                return 0 if ok else 1
            if server is None and (data_dir / "latest.json").is_file():
                server, port = start_server(ROOT / "web", data_dir, host="0.0.0.0" if args.share else "127.0.0.1")  # noqa: S104 - 사용자가 --share 를 준 때만
                url = f"http://127.0.0.1:{port}/"
                print(f"\n화면 주소: {url}  (이 컴퓨터)")
                if args.share:
                    address = lan_address()
                    print(f"같은 네트워크의 다른 기기: http://{address}:{port}/" if address else "같은 네트워크 주소를 알아내지 못했습니다(이 컴퓨터의 IP 주소:포트 로 접속하세요).")
                    print("  ※ 윈도우가 '액세스 허용'을 물으면 허용해야 다른 기기에서 열립니다. 같은 네트워크의 누구나 볼 수 있습니다(보기 전용).")
                else:
                    print("  (다른 기기에서도 보려면 start-share.bat 으로 실행하세요)")
                if not args.no_browser:
                    webbrowser.open(url)
            if args.once or args.demo:
                if server is None:
                    return 1
                print("\n이 창을 닫으면 화면도 닫힙니다. (한 번만 수집 모드)")
                while True:
                    time.sleep(3600)
            if ok:
                rail_once(cfg, data_dir, env)
            pause = timedelta(hours=INTERVAL_HOURS) if ok else timedelta(minutes=RETRY_MINUTES)
            nxt = now_kst() + pause
            print(f"\n다음 예측 계산: {nxt:%H:%M}. 이 창을 열어 두면 실시간 자료는 계속 갱신됩니다. 닫으면 멈춥니다.\n", flush=True)
            wait_until(nxt)
    except KeyboardInterrupt:
        print("\n종료합니다.")
        return 0
    finally:
        stop.set()
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
