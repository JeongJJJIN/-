"""기상청 특보구역 코드표 (config/warning_zones.json).

특보 통보문은 구역을 이름으로 적는다("전라남도(순천, 완도(여서도 제외))", "울릉도.독도", "서울").
이 모듈은 그 이름을 구역 코드로 바꾸고, 상위 구역(도·광역시)을 가장 작은 단위의 구역(말단 구역)들로 펼친다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_DIR, ConfigError


@dataclass(frozen=True)
class Zone:
    code: str
    up: str | None
    name: str  # 통보문에 쓰이는 약칭 (순천, 고흥북부, 전라남도 …)
    full: str  # 정식 이름 (순천시, 고흥군북부 …)

    @property
    def is_land(self) -> bool:
        return self.code.startswith("L")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


class ZoneTable:
    def __init__(self, zones: list[Zone], aliases: dict[str, str] | None = None):
        self.by_code: dict[str, Zone] = {z.code: z for z in zones}
        self._children: dict[str, list[str]] = {}
        for z in zones:
            if z.up:
                self._children.setdefault(z.up, []).append(z.code)
        self._by_name: dict[str, list[str]] = {}
        for z in zones:
            for key in {_norm(z.name), _norm(z.full)}:
                if key:
                    self._by_name.setdefault(key, []).append(z.code)
        self._aliases = {_norm(k): _norm(v) for k, v in (aliases or {}).items()}
        # 최상위 묶음(전국·전해상)과 그 바로 아래(도·광역시·해역)
        self._roots = {z.code for z in zones if z.up is None}

    def __len__(self) -> int:
        return len(self.by_code)

    def name(self, code: str) -> str:
        zone = self.by_code.get(code)
        return zone.name if zone else code

    def is_top_level(self, code: str) -> bool:
        zone = self.by_code.get(code)
        return zone is not None and (zone.up is None or zone.up in self._roots)

    def leaves(self, code: str) -> list[str]:
        """이 구역에 속한 말단 구역 코드들(자신이 말단이면 자신)."""
        kids = self._children.get(code)
        if not kids:
            return [code] if code in self.by_code else []
        out: list[str] = []
        for kid in kids:
            out.extend(self.leaves(kid))
        return out

    def leaf_codes(self, land_only: bool = False) -> list[str]:
        return [c for c in self.by_code if c not in self._children and (not land_only or c.startswith("L"))]

    def descendants(self, code: str) -> list[str]:
        out: list[str] = []
        for kid in self._children.get(code, []):
            out.append(kid)
            out.extend(self.descendants(kid))
        return out

    def _lookup(self, name: str) -> list[str]:
        key = _norm(name)
        key = self._aliases.get(key, key)
        return list(self._by_name.get(key, []))

    def find(self, name: str, parent: str | None = None) -> list[str]:
        """이름에 해당하는 구역 코드(펼치기 전). parent 가 있으면 그 상위 구역 아래에서만 찾는다.

        상위 구역 없이 쓰인 이름이 여러 곳에 있으면(예: '광주' = 광주광역시 / 경기 광주) 도·광역시급을 택한다.
        """
        matches = self._lookup(name)
        if parent is not None:
            scope: set[str] = set()
            for p in self._lookup(parent):
                scope.update(self.descendants(p))
            return [c for c in matches if c in scope]
        if len(matches) <= 1:
            return matches
        top = [c for c in matches if self.is_top_level(c)]
        if top:
            return top
        # 같은 이름의 묶음과 그 말단(예: 울릉도.독도)이면 같은 곳이다
        groups = {tuple(sorted(self.leaves(c))) for c in matches}
        return matches[:1] if len(groups) == 1 else []

    def resolve(self, name: str, parent: str | None = None) -> list[str]:
        """이름 → 말단 구역 코드 목록(중복 없이, 표의 순서대로). 찾지 못하면 빈 목록."""
        out: list[str] = []
        for code in self.find(name, parent):
            for leaf in self.leaves(code):
                if leaf not in out:
                    out.append(leaf)
        return out


def load_zone_table(path: Path | None = None) -> ZoneTable:
    path = path or CONFIG_DIR / "warning_zones.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"설정 파일이 없습니다: {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"JSON 형식 오류: {path}: {e}") from e
    zones = [Zone(code=z["code"], up=z.get("up"), name=z["name"], full=z.get("full") or z["name"]) for z in data.get("zones", [])]
    if not zones:
        raise ConfigError("warning_zones.json: zones 가 비어 있습니다")
    codes = {z.code for z in zones}
    for z in zones:
        if z.up and z.up not in codes:
            raise ConfigError(f"warning_zones.json: {z.code} 의 상위 구역 {z.up} 이 표에 없습니다")
    return ZoneTable(zones, data.get("aliases"))
