"""내 컴퓨터에서만 쓰기: 수집 → 계산 → 이 컴퓨터의 브라우저로 보기.

GitHub 없이 돌리는 방법이다. 윈도우는 start.bat, 맥은 start.command 를 두 번 누르면 이 모듈이 실행된다.

    python -m teukbo.local            3시간마다 다시 수집하며 계속 실행 (창을 닫으면 멈춤)
    python -m teukbo.local --once     한 번만 수집
    python -m teukbo.local --demo     인터넷·인증키 없이 가상 자료로 화면만 보기

- 화면은 이 컴퓨터에서만 열린다(http://127.0.0.1:포트). 다른 사람·휴대폰에서는 볼 수 없다.
- 인증키는 처음 한 번 물어보고 local_keys.txt 에 저장한다(이 컴퓨터에만 남고 저장소에는 올라가지 않는다).
- 이력은 data/history 에 쌓인다.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import run as run_mod
from . import storage
from .config import ROOT, Config, load_config
from .timeutil import now_kst

KEY_FILE = ROOT / "local_keys.txt"
KEY_NAMES = ("DATA_GO_KR_SERVICE_KEY", "KMA_APIHUB_KEY")
INTERVAL_HOURS = 3
RETRY_MINUTES = 30
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
    return {"DATA_GO_KR_SERVICE_KEY": data_key, "KMA_APIHUB_KEY": ""}


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


def start_server(web_dir: Path, data_dir: Path, ports: range = PORTS) -> tuple[ThreadingHTTPServer, int]:
    """127.0.0.1 에서만 열리는 서버를 띄운다(다른 컴퓨터에서는 접속할 수 없다)."""
    handler = make_handler(web_dir, data_dir)
    last_error: OSError | None = None
    for port in ports:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as e:
            last_error = e
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, port
    raise OSError(f"사용할 수 있는 포트가 없습니다 ({ports.start}~{ports.stop - 1}): {last_error}")


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
    parser = argparse.ArgumentParser(description="기상특보 가능성 - 내 컴퓨터에서만 쓰기")
    parser.add_argument("--once", action="store_true", help="한 번만 수집하고 화면을 띄운 채로 둔다")
    parser.add_argument("--demo", action="store_true", help="인터넷·인증키 없이 가상 자료로 화면만 본다")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않는다")
    parser.add_argument("--exit-after-collect", action="store_true", help=argparse.SUPPRESS)  # 시험용
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):  # 일부 윈도우 콘솔에서 표시 못 하는 글자가 있어도 멈추지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.ERROR, format="  (%(name)s) %(message)s", stream=sys.stderr)
    logging.getLogger("teukbo.http").setLevel(logging.CRITICAL)  # 재시도 과정은 생략하고 최종 결과만 보여 준다

    print("기상특보 가능성 - 내 컴퓨터용")
    print("참고용입니다. 공식 특보·예비특보가 우선합니다.")
    if args.demo:
        print("※ 데모 모드: 아래 수치는 실제 예보가 아닌 가상 자료입니다.")
    cfg = load_config()
    data_dir = ROOT / ("data-demo" if args.demo else "data")
    env = dict(os.environ)
    if not args.demo:
        keys = read_keys(KEY_FILE)
        if not KEY_FILE.exists() and not env.get("DATA_GO_KR_SERVICE_KEY") and sys.stdin and sys.stdin.isatty():
            keys = ask_keys()
            write_keys(KEY_FILE, keys)
            print(f"저장했습니다: {KEY_FILE.name} (바꾸려면 이 파일을 고치거나 지우세요)")
        for name, value in keys.items():
            if value and not env.get(name):
                env[name] = value

    server = None
    try:
        while True:
            ok = collect_once(cfg, data_dir, env, args.demo)
            if args.exit_after_collect:
                return 0 if ok else 1
            if server is None and (data_dir / "latest.json").is_file():
                server, port = start_server(ROOT / "web", data_dir)
                url = f"http://127.0.0.1:{port}/"
                print(f"\n화면 주소: {url}  (이 컴퓨터에서만 열립니다)")
                if not args.no_browser:
                    webbrowser.open(url)
            if args.once or args.demo:
                if server is None:
                    return 1
                print("\n이 창을 닫으면 화면도 닫힙니다. (한 번만 수집 모드)")
                while True:
                    time.sleep(3600)
            pause = timedelta(hours=INTERVAL_HOURS) if ok else timedelta(minutes=RETRY_MINUTES)
            nxt = now_kst() + pause
            print(f"\n다음 갱신: {nxt:%H:%M}. 이 창을 열어 두면 자동으로 다시 받습니다. 닫으면 멈춥니다.\n", flush=True)
            wait_until(nxt)
    except KeyboardInterrupt:
        print("\n종료합니다.")
        return 0
    finally:
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
