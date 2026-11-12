"""배포용 정적 사이트 조립: web/ + 최신 결과 JSON → _site/

    python -m teukbo.site                       (data/ 사용)
    python -m teukbo.site --data-dir data-demo  (데모 자료로 미리보기)
    python -m http.server -d _site 8000         (로컬 확인)

web/ 파일 내용으로 빌드 번호를 만들어 sw.js·index.html 의 __BUILD__ 를 바꿔 넣는다.
화면 파일이 바뀌면 번호가 달라져 설치된 앱(PWA)의 캐시가 새로 갈린다.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import sys
from pathlib import Path

from .config import ROOT

log = logging.getLogger("teukbo.site")

BUILD_TOKEN = "__BUILD__"
_TEXT_SUFFIXES = {".html", ".js", ".css", ".webmanifest", ".json"}


def build_id(web_dir: Path) -> str:
    digest = hashlib.sha1()
    for path in sorted(p for p in web_dir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(web_dir).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def build_site(data_dir: Path, out_dir: Path, web_dir: Path | None = None) -> str:
    web_dir = web_dir or ROOT / "web"
    latest = data_dir / "latest.json"
    if not latest.is_file():
        raise FileNotFoundError(f"{latest} 가 없습니다. 먼저 `python -m teukbo.run` 을 실행하세요.")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(web_dir, out_dir)

    build = build_id(web_dir)
    for path in out_dir.rglob("*"):
        if path.is_file() and path.suffix in _TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8")
            if BUILD_TOKEN in text:
                path.write_text(text.replace(BUILD_TOKEN, build), encoding="utf-8")

    site_data = out_dir / "data"
    site_data.mkdir(exist_ok=True)
    shutil.copy2(latest, site_data / "latest.json")
    for name in ("accuracy.json", "warnings.json", "live.json"):  # live.json 은 데모·내 컴퓨터용(배포본은 따로 올라온다)
        if (data_dir / name).is_file():
            shutil.copy2(data_dir / name, site_data / name)
    if (data_dir / "rail" / "summary.json").is_file():
        shutil.copy2(data_dir / "rail" / "summary.json", site_data / "rail.json")
    # 분석 화면(과거 자료 탐색): 묶음 집계와, 날짜를 골랐을 때 읽는 그날의 열차 목록
    days_dir = data_dir / "rail" / "days"
    if (data_dir / "rail" / "cube.json").is_file():
        shutil.copy2(data_dir / "rail" / "cube.json", site_data / "rail-cube.json")
    elif days_dir.is_dir() and any(days_dir.glob("*.json")):
        # 묶음이 아직 없으면(수집이 중간에 멈춘 회차 등) 쌓여 있는 날짜별 기록으로 여기서 만든다 — 분석 화면이 비지 않게
        from . import rail, rail_run, storage

        storage.write_json(site_data / "rail-cube.json", rail.build_cube(rail_run.load_days(data_dir)))
    if days_dir.is_dir():
        shutil.copytree(days_dir, site_data / "rail-days")
    if (data_dir / "detail").is_dir():
        shutil.copytree(data_dir / "detail", site_data / "detail")
    return build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="배포용 정적 사이트 조립")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--out", type=Path, default=ROOT / "_site")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        build = build_site(args.data_dir, args.out)
    except FileNotFoundError as e:
        log.error("%s", e)
        return 1
    log.info("사이트 조립 완료: %s (빌드 %s)", args.out, build)
    return 0


if __name__ == "__main__":
    sys.exit(main())
