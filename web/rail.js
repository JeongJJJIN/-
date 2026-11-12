/* 철도 특보 지도 — 전국 특보 구역(현재) + 철도 노선·역, 그리고 전날까지의 열차 지연 통계.
 * 지도는 외부 지도 서비스 없이 geo/*.json(특보구역·노선·역)을 직접 그린다.
 * 열차의 실시간 위치는 공개 자료가 없어 표시하지 않는다(코레일 공식 화면으로 연결).
 */
(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const OFFICIAL_URL = "https://www.weather.go.kr/w/special-report/overall.do";
  const GRADE_LABEL = { caution: "주의", high: "높음" };
  const REFRESH_MS = 10 * 60 * 1000;
  // 역 이름을 항상 보여 줄 주요 역(넓게 볼 때). 확대하면 더 많은 역 이름이 나온다.
  const HUBS = new Set(["서울", "대전", "동대구", "부산", "광주송정", "목포", "익산", "순천", "여수엑스포", "강릉", "포항", "울산", "전주", "청량리", "천안아산", "오송", "원주", "안동", "진주", "마산", "춘천"]);
  const SECOND = new Set(["용산", "수원", "평택", "천안", "조치원", "서대전", "김천(구미)", "구미", "경주", "밀양", "구포", "창원중앙", "나주", "정읍", "남원", "구례구", "곡성", "광양", "보성", "벌교", "제천", "영주", "동해", "태백", "홍성", "논산", "광명", "행신", "S수서", "동탄", "신경주", "영암", "강진", "해남", "하동", "함안", "영덕", "울진", "삼척", "정동진", "평창", "진부(오대산)", "충주", "단양", "풍기", "김천", "영동", "옥천", "장성", "함평", "무안", "일로", "몽탄", "화순", "능주", "이양", "득량", "예당", "조성", "서광주", "효천", "남평", "전남장흥", "신보성"]);

  // 투영: 위도 36° 기준의 단순 등거리 투영 (우리나라 범위에서는 왜곡이 작다)
  const K = 100, COS = Math.cos((36 * Math.PI) / 180), LON0 = 124.5, LAT1 = 38.9;
  const X = (lon) => (lon - LON0) * COS * K;
  const Y = (lat) => (LAT1 - lat) * K;
  const box = (lon0, lat0, lon1, lat1) => ({ x: X(lon0), y: Y(lat1), w: X(lon1) - X(lon0), h: Y(lat0) - Y(lat1) });
  const FULL = box(125.6, 33.05, 130.0, 38.65);
  const LIMIT = box(123.8, 32.4, 132.4, 39.2);
  const JEONNAM = box(126.05, 34.0, 128.3, 35.5);

  const state = { zones: null, rail: null, stations: null, warnings: null, latest: null, summary: null, type: "all", view: { ...FULL }, picked: null, svg: null };

  // ------------------------------------------------------------ 도우미

  function build(node, attrs, children) {
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value == null || value === false) continue;
      if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat(Infinity)) {
      if (child == null || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }
  const h = (tag, attrs, ...children) => build(document.createElement(tag), attrs, children);
  const s = (tag, attrs, ...children) => build(document.createElementNS(SVG_NS, tag), attrs, children);
  const $ = (id) => document.getElementById(id);
  const clear = (node) => { node.replaceChildren(); return node; };

  const KST = new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", month: "numeric", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  function kst(iso) {
    const out = {};
    for (const part of KST.formatToParts(new Date(iso))) out[part.type] = part.value;
    return { month: Number(out.month), day: Number(out.day), weekday: out.weekday, hour: out.hour, minute: out.minute };
  }
  const fmtClock = (iso) => { const p = kst(iso); return `${p.month}월 ${p.day}일 ${p.hour}:${p.minute}`; };
  const fmtDayHour = (iso) => { const p = kst(iso); return `${p.day}일(${p.weekday}) ${p.hour}시`; };
  const fmtDate = (ymd) => { const [, m, d] = ymd.split("-"); return `${Number(m)}월 ${Number(d)}일`; };
  function fmtAgo(iso) {
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 1) return "방금";
    if (minutes < 60) return `${minutes}분 전`;
    const hours = Math.floor(minutes / 60);
    return hours < 48 ? `${hours}시간 ${minutes % 60 ? `${minutes % 60}분 ` : ""}전` : `${Math.floor(hours / 24)}일 전`;
  }
  const num = (value, digits = 1) => (value == null ? "–" : Number(value).toFixed(digits).replace(/\.0+$/, ""));
  const share = (part, total) => {
    if (!total) return "–";
    const pct = (part / total) * 100;
    return part > 0 && pct < 1 ? "1% 미만" : `${Math.round(pct)}%`;
  };
  const stationLabel = (name) => name.replace(/^S(?=[가-힣])/, "");
  const typeOf = (name) => name.replace(/\s*예비특보$/, "").replace(/(중대경보|주의보|경보)$/, "");
  const isWarningLevel = (name) => /경보$/.test(name);

  async function getJson(path, fresh) {
    const response = await fetch(path, fresh ? { cache: "no-cache" } : undefined);
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    return response.json();
  }

  // ------------------------------------------------------------ 특보 상태

  /** 구역별 공식 특보(고른 종류만). → Map(code → {active: [...], pre: [...]}) */
  function officialByZone() {
    const out = new Map();
    const w = state.warnings;
    if (!w || !w.available) return out;
    const want = (name) => state.type === "all" || typeOf(name) === state.type;
    const slot = (code) => { if (!out.has(code)) out.set(code, { active: [], pre: [] }); return out.get(code); };
    for (const a of w.active) if (want(a.name)) slot(a.code).active.push(a);
    for (const p of w.preliminary) if (want(p.name)) slot(p.code).pre.push(p);
    return out;
  }

  /** 구역별 가능성 등급(계산 대상 지역만). → Map(code → {grade, region, hazard, p}) */
  function possibilityByZone() {
    const out = new Map();
    const latest = state.latest;
    if (!latest) return out;
    const typeName = Object.fromEntries(latest.types.map((t) => [t.id, t.name]));
    const hazards = latest.hazards.filter((hz) => state.type === "all" || typeName[hz.type] === state.type);
    const rank = { caution: 1, high: 2 };
    for (const region of latest.regions) {
      let best = null;
      for (const hz of hazards) {
        const sum = region.hazards[hz.id];
        if (!sum || !rank[sum.mg]) continue;  // 공식 발표를 반영하기 전의 계산 등급
        if (!best || rank[sum.mg] > rank[best.grade] || (rank[sum.mg] === rank[best.grade] && (sum.p || 0) > (best.p || 0))) best = { grade: sum.mg, hazard: hz.name, p: sum.p };
      }
      if (!best) continue;
      for (const code of region.zones || []) {
        const prev = out.get(code);
        if (!prev || rank[best.grade] > rank[prev.grade]) out.set(code, { ...best, region });
      }
    }
    return out;
  }

  function zoneClass(code, official, possibility) {
    const off = official.get(code);
    let cls = "zone";
    if (off && off.active.some((a) => isWarningLevel(a.name))) cls += " z-warn";
    else if (off && off.active.length) cls += " z-adv";
    else if (off && off.pre.length) cls += " z-pre";
    const poss = possibility.get(code);
    if (poss) cls += poss.grade === "high" ? " p-high" : " p-caution";
    if (state.picked && state.picked.kind === "zone" && state.picked.code === code) cls += " picked";
    return cls;
  }

  // ------------------------------------------------------------ 지도

  const pathOf = (rings) => rings.map((r) => { let d = ""; for (let i = 0; i < r.length; i += 2) d += `${i ? "L" : "M"}${X(r[i]).toFixed(2)} ${Y(r[i + 1]).toFixed(2)}`; return `${d}Z`; }).join("");
  const lineOf = (pts) => { let d = ""; for (let i = 0; i < pts.length; i += 2) d += `${i ? "L" : "M"}${X(pts[i]).toFixed(2)} ${Y(pts[i + 1]).toFixed(2)}`; return d; };

  function drawMap() {
    const holder = clear($("map"));
    const svg = s("svg", { role: "img", "aria-label": "전국 기상특보 구역과 철도 노선 지도. 구역별 특보는 아래 ‘특보 발효 구역 목록’에서 글로 볼 수 있습니다.", tabindex: "0" });
    // 무늬: 빗금 = 예비특보, 점 = 계산한 가능성(높음·주의). 색만으로 구분하지 않으려는 것이다.
    // 크기는 확대 정도에 맞춰 setView 에서 다시 정한다.
    const dots = (id) => s("pattern", { id, patternUnits: "userSpaceOnUse" }, s("rect", { class: `${id}-bg` }), s("circle", { class: `${id}-dot` }));
    state.patterns = [
      s("pattern", { id: "hatch", patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, s("rect", { class: "hatch-bg" }), s("line", { class: "hatch-line" })),
      dots("dot-high"), dots("dot-caution"),
    ];
    const zones = s("g", { class: "zones" });
    for (const [code, zone] of Object.entries(state.zones)) zones.append(s("path", { d: pathOf(zone.p), "data-zone": code }));
    const casing = s("g", { class: "rail-cases" }), rail = s("g", { class: "rails" });
    for (const line of state.rail) {
      for (const pts of line.paths) {
        const d = lineOf(pts);
        casing.append(s("path", { d, class: line.fast ? "rail-case fast" : "rail-case" }));
        rail.append(s("path", { d, class: line.fast ? "rail fast" : "rail" }));
      }
    }
    const marks = s("g", { class: "stations" });
    const labels = s("g", { class: "labels" });
    for (const st of state.stations) {
      st.rank = HUBS.has(st.name) ? 0 : SECOND.has(st.name) ? 1 : 2;
      st.key = st.code || `name:${st.name}`;
      st.label = stationLabel(st.name);
      st.x = X(st.lon);
      st.y = Y(st.lat);
      marks.append(s("circle", { cx: st.x.toFixed(2), cy: st.y.toFixed(2), r: st.rank === 0 ? 3.4 : st.rank === 1 ? 2.6 : 2.2, class: `st r${st.rank}`, "data-st": st.key }));
      st.text = s("text", { x: st.x.toFixed(2), y: st.y.toFixed(2), class: `lb r${st.rank}`, "data-st": st.key }, st.label);
      labels.append(st.text);
    }
    state.byRank = [...state.stations].sort((a, b) => a.rank - b.rank);
    svg.append(s("defs", null, state.patterns), zones, casing, rail, marks, labels);
    holder.append(svg);
    state.svg = svg;
    bindMapEvents(svg);
    paintZones();
    setView(state.view);
  }

  function paintZones() {
    if (!state.svg) return;
    const official = officialByZone(), possibility = possibilityByZone();
    let front = null;
    for (const path of state.svg.querySelectorAll("path[data-zone]")) {
      const cls = zoneClass(path.dataset.zone, official, possibility);
      path.setAttribute("class", cls);
      // 무늬는 인라인 스타일로 넣는다(바깥 CSS 파일의 url(#…) 은 브라우저에 따라 이 문서를 가리키지 않는다).
      // 공식 발표(발효·예비)가 계산한 가능성보다 먼저다.
      const official_ = /\bz-(warn|adv|pre)\b/.exec(cls);
      path.style.fill = official_ ? (official_[1] === "pre" ? "url(#hatch)" : "") : cls.includes(" p-high") ? "url(#dot-high)" : cls.includes(" p-caution") ? "url(#dot-caution)" : "";
      if (cls.endsWith(" picked")) front = path;
    }
    if (front) front.parentNode.append(front);  // 고른 구역의 테두리가 이웃 구역에 가리지 않게
    for (const el of state.svg.querySelectorAll(".st.picked, .lb.picked")) el.classList.remove("picked");
    if (state.picked && state.picked.kind === "station") {
      for (const el of state.svg.querySelectorAll(`[data-st="${CSS.escape(state.picked.key)}"]`)) el.classList.add("picked");
    }
    if (state.labelU) placeLabels(state.labelU, state.zoom);
  }

  /** 보이는 범위를 정한다. 화면 비율에 맞춰 넓히고, 전체 한계 밖으로 나가지 않게 한다. */
  function setView(target) {
    const svg = state.svg;
    const rect = svg.getBoundingClientRect();
    const ratio = rect.height / rect.width || 1;
    let { x, y, w, h: hh } = target;
    const cx = x + w / 2, cy = y + hh / 2;
    if (hh / w < ratio) hh = w * ratio; else w = hh / ratio;
    const maxW = Math.min(LIMIT.w, LIMIT.h / ratio), minW = 8;
    if (w > maxW) { w = maxW; hh = w * ratio; }
    if (w < minW) { w = minW; hh = w * ratio; }
    x = Math.min(Math.max(cx - w / 2, LIMIT.x), LIMIT.x + LIMIT.w - w);
    y = Math.min(Math.max(cy - hh / 2, LIMIT.y), LIMIT.y + LIMIT.h - hh);
    state.view = { x, y, w, h: hh };
    svg.setAttribute("viewBox", `${x.toFixed(2)} ${y.toFixed(2)} ${w.toFixed(2)} ${hh.toFixed(2)}`);
    const u = w / (rect.width || 1);  // 화면 1px 이 지도 좌표로 얼마인지
    svg.style.setProperty("--u", u.toFixed(5));
    const tile = (7 * u).toFixed(3);
    const set = (node, attrs) => { for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value); };
    for (const pattern of state.patterns) {
      const [bg, mark] = pattern.children;
      set(pattern, { width: tile, height: tile });
      set(bg, { width: tile, height: tile });
      if (mark.tagName === "line") set(mark, { x1: 0, y1: 0, x2: 0, y2: tile, "stroke-width": (5 * u).toFixed(3) });
      else set(mark, { cx: (3.5 * u).toFixed(3), cy: (3.5 * u).toFixed(3), r: (1.7 * u).toFixed(3) });
    }
    const zoom = FULL.w / w;
    svg.classList.toggle("zoom1", zoom >= 1.7);
    svg.classList.toggle("zoom2", zoom >= 3.6);
    if (Math.abs(u - (state.labelU || 0)) > u * 0.01) { state.labelU = u; state.zoom = zoom; placeLabels(u, zoom); }
  }

  /** 역 이름이 서로 겹치지 않게, 중요한 역부터 자리를 주고 겹치는 이름은 숨긴다. (옮길 때는 그대로, 확대가 바뀔 때만 다시 계산) */
  function placeLabels(u, zoom) {
    const maxRank = zoom >= 3.6 ? 2 : zoom >= 1.7 ? 1 : 0;
    const pickedKey = state.picked && state.picked.kind === "station" ? state.picked.key : null;
    const placed = [];
    const order = pickedKey ? [...state.byRank].sort((a, b) => (b.key === pickedKey) - (a.key === pickedKey)) : state.byRank;  // 고른 역의 이름은 늘 보인다
    for (const st of order) {
      let show = st.rank <= maxRank || st.key === pickedKey;
      if (show) {
        // 화면 px 단위의 글자 상자(한글 한 자 ≈ 글자 크기). 역 표시 오른쪽에 놓인다.
        const x0 = st.x / u + 4, y0 = st.y / u - 8, x1 = x0 + st.label.length * 11.5 + 6, y1 = y0 + 15;
        show = !placed.some((b) => x0 < b[2] && x1 > b[0] && y0 < b[3] && y1 > b[1]);
        if (show) placed.push([x0, y0, x1, y1]);
      }
      st.text.classList.toggle("show", show);
    }
  }

  function zoomBy(factor, clientX, clientY) {
    const svg = state.svg, rect = svg.getBoundingClientRect(), v = state.view;
    const fx = clientX == null ? 0.5 : (clientX - rect.left) / rect.width;
    const fy = clientY == null ? 0.5 : (clientY - rect.top) / rect.height;
    const w = v.w / factor, hh = v.h / factor;
    setView({ x: v.x + (v.w - w) * fx, y: v.y + (v.h - hh) * fy, w, h: hh });
  }

  function bindMapEvents(svg) {
    const pointers = new Map();
    let start = null, moved = 0;
    const begin = () => {
      const pts = [...pointers.values()];
      start = { view: { ...state.view }, pts: pts.map((p) => ({ ...p })) };
    };
    svg.addEventListener("pointerdown", (event) => {
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 1) moved = 0;
      begin();
    });
    window.addEventListener("pointermove", (event) => {
      if (!pointers.has(event.pointerId) || !start) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      const rect = svg.getBoundingClientRect(), scale = start.view.w / rect.width;
      const pts = [...pointers.values()];
      if (pts.length === 1) {
        const dx = pts[0].x - start.pts[0].x, dy = pts[0].y - start.pts[0].y;
        moved = Math.max(moved, Math.hypot(dx, dy));
        if (moved > 4) setView({ ...start.view, x: start.view.x - dx * scale, y: start.view.y - dy * scale });
      } else if (pts.length >= 2 && start.pts.length >= 2) {
        moved = 99;
        const d0 = Math.hypot(start.pts[0].x - start.pts[1].x, start.pts[0].y - start.pts[1].y) || 1;
        const d1 = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
        const factor = d1 / d0, w = start.view.w / factor, hh = start.view.h / factor;
        const mx0 = (start.pts[0].x + start.pts[1].x) / 2, my0 = (start.pts[0].y + start.pts[1].y) / 2;
        const mx1 = (pts[0].x + pts[1].x) / 2, my1 = (pts[0].y + pts[1].y) / 2;
        // 처음 두 손가락 가운데가 가리키던 지점이 지금 가운데 아래에 오도록
        const gx = start.view.x + (mx0 - rect.left) * scale, gy = start.view.y + (my0 - rect.top) * scale;
        const s2 = w / rect.width;
        setView({ x: gx - (mx1 - rect.left) * s2, y: gy - (my1 - rect.top) * s2, w, h: hh });
      }
    });
    const end = (event) => { if (pointers.delete(event.pointerId)) begin(); };
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    // 그냥 휠은 페이지를 넘기는 데 쓰게 두고, Ctrl+휠(트랙패드 오므리기 포함)일 때만 지도를 확대한다
    svg.addEventListener("wheel", (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      zoomBy(event.deltaY < 0 ? 1.25 : 0.8, event.clientX, event.clientY);
    }, { passive: false });
    svg.addEventListener("click", (event) => {
      if (moved > 6) return;  // 끌어서 옮긴 것은 누른 것으로 치지 않는다
      // 역은 점이 작으므로, 누른 곳에서 14px 안의 가장 가까운(지금 보이는) 역을 먼저 찾는다
      const rect = svg.getBoundingClientRect(), u = state.view.w / rect.width;
      const mx = state.view.x + (event.clientX - rect.left) * u, my = state.view.y + (event.clientY - rect.top) * u;
      const maxRank = svg.classList.contains("zoom1") ? 2 : 1;
      let best = null, bestD = 14 * u;
      for (const st of state.stations) {
        if (st.rank > maxRank) continue;
        const d = Math.hypot(st.x - mx, st.y - my);
        if (d < bestD) { best = st; bestD = d; }
      }
      const zone = event.target.closest("[data-zone]");
      if (best) pick({ kind: "station", key: best.key });
      else if (zone) pick({ kind: "zone", code: zone.dataset.zone });
    });
    svg.addEventListener("keydown", (event) => {
      const step = state.view.w * 0.15;
      const move = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[event.key];
      if (move) { event.preventDefault(); setView({ ...state.view, x: state.view.x + move[0], y: state.view.y + move[1] }); }
      else if (event.key === "+" || event.key === "=") zoomBy(1.5);
      else if (event.key === "-") zoomBy(1 / 1.5);
    });
    window.addEventListener("resize", () => setView(state.view));
  }

  // ------------------------------------------------------------ 고른 구역·역

  const zoneFullName = (code) => {
    const zone = state.zones[code];
    return zone ? zone.n : code;
  };

  function describeOfficial(off) {
    const items = [];
    for (const a of (off && off.active) || []) {
      const upcoming = a.from && new Date(a.from).getTime() > Date.now();
      items.push(h("li", null, h("span", { class: `pill ${isWarningLevel(a.name) ? "warn" : "adv"}`, text: upcoming ? "발효 예정" : "발효 중" }), " ", h("b", { text: a.name }), a.from ? ` · ${fmtDayHour(a.from)} ${upcoming ? "발효 예정" : "발효"}` : ""));
    }
    for (const p of (off && off.pre) || []) items.push(h("li", null, h("span", { class: "pill pre", text: "예비특보" }), " ", h("b", { text: p.name }), p.expected ? ` · 예상 ${p.expected}` : ""));
    return items;
  }

  function pick(target) {
    state.picked = target;
    paintZones();
    renderPicked();
  }

  function renderPicked() {
    const panel = clear($("picked"));
    const target = state.picked;
    if (!target) { panel.append(h("p", { class: "empty", text: "지도에서 구역이나 역을 누르면 여기에 특보 내용이 나옵니다." })); return; }
    const official = officialByZone(), possibility = possibilityByZone();
    let code = target.code, title;
    if (target.kind === "station") {
      const st = state.stations.find((x) => x.key === target.key);
      if (!st) return;
      code = st.zone;
      title = h("h3", null, `${st.label}역`, h("small", null, ` ${zoneFullName(code)} 특보구역`));
    } else {
      title = h("h3", null, zoneFullName(code), h("small", null, " 특보구역"));
    }
    panel.append(title);
    const available = state.warnings && state.warnings.available;
    const items = describeOfficial(official.get(code));
    if (!available) panel.append(h("p", { class: "empty" }, "특보 현황을 확인하지 못했습니다. ", h("a", { href: OFFICIAL_URL, rel: "noopener" }, "기상청 날씨누리에서 확인하세요.")));
    else if (items.length) panel.append(h("ul", { class: "plain" }, items));
    else panel.append(h("p", { class: "empty", text: state.type === "all" ? "발효 중인 특보·예비특보가 없습니다." : `${state.type} 특보·예비특보가 없습니다.` }));
    const poss = possibility.get(code);
    const region = poss ? poss.region : (state.latest ? state.latest.regions.find((r) => (r.zones || []).includes(code)) : null);
    if (poss) panel.append(h("p", { class: "poss" }, h("span", { class: `pill ${poss.grade === "high" ? "p-high" : "p-caution"}`, text: `가능성 ${GRADE_LABEL[poss.grade]}` }), ` ${poss.hazard} ${poss.p}% (향후 ${state.latest.horizon_hours}시간, 계산값)`));
    if (region) panel.append(h("p", null, h("a", { href: `./#${region.id}` }, `${region.name} 특보 가능성 자세히 보기`)));
    const here = state.stations.filter((x) => x.zone === code).map((x) => x.label);
    if (target.kind === "zone" && here.length) panel.append(h("p", { class: "note", text: `이 구역의 역: ${[...new Set(here)].join(", ")}` }));
  }

  // ------------------------------------------------------------ 머리·종류 고르기·범례·목록

  function renderHead() {
    const w = state.warnings, latest = state.latest;
    const when = (w && w.generated_at) || (latest && latest.generated_at);
    $("updated").textContent = when ? `${fmtClock(when)} 갱신 · ${fmtAgo(when)}` : "";
    const alerts = clear($("alerts"));
    const add = (kind, ...content) => alerts.append(h("div", { class: `banner ${kind}`, role: "status" }, h("div", null, ...content)));
    if (latest && latest.demo) add("demo", "데모 화면입니다. 표시된 내용은 실제가 아닌 가상 자료입니다.");
    if (w && !w.available) add("", h("strong", null, "기상청 특보 현황을 불러오지 못했습니다. "), "지도에 특보가 표시되지 않습니다. ", h("a", { href: OFFICIAL_URL, rel: "noopener" }, "기상청 날씨누리"), "에서 확인하세요.");
    if (when && !(latest && latest.demo) && (Date.now() - new Date(when).getTime()) / 3600000 > ((latest && latest.stale_after_hours) || 7)) {
      add("", h("strong", null, `자료가 ${fmtAgo(when)} 것입니다. `), "갱신이 늦어지고 있어 현재 상황과 다를 수 있습니다.");
    }
  }

  function renderTypeChips() {
    const box = clear($("type-chips"));
    const w = state.warnings;
    const names = new Set();
    if (w && w.available) for (const item of [...w.active, ...w.preliminary]) names.add(typeOf(item.name));
    if (state.latest) for (const t of state.latest.types) names.add(t.name);
    const order = ["호우", "태풍", "강풍", "대설", "한파", "폭염", "열대야", "건조", "풍랑", "황사", "폭풍해일"];
    const list = ["all", ...[...names].sort((a, b) => (order.indexOf(a) + 99) % 99 - (order.indexOf(b) + 99) % 99)];
    if (!list.includes(state.type)) state.type = "all";
    for (const name of list) {
      box.append(h("button", { class: "chip", type: "button", "aria-pressed": String(state.type === name), onclick: () => { state.type = name; renderTypeChips(); paintZones(); renderPicked(); renderList(); } }, name === "all" ? "전체" : name));
    }
  }

  function renderLegend() {
    const key = (cls, text) => h("li", null, h("span", { class: `swatch ${cls}` }), text);
    clear($("legend")).append(
      key("z-warn", "경보 발효"), key("z-adv", "주의보 발효"), key("z-pre", "예비특보"),
      key("p-high", "가능성 높음(계산값)"), key("p-caution", "가능성 주의(계산값)"),
      h("li", null, h("span", { class: "swatch line" }), "철도"), h("li", null, h("span", { class: "swatch line fast" }), "고속선"));
  }

  function renderList() {
    const panel = clear($("warning-list"));
    const w = state.warnings;
    if (!w || !w.available) { panel.append(h("p", { class: "empty" }, "특보 현황을 확인하지 못했습니다. ", h("a", { href: OFFICIAL_URL, rel: "noopener" }, "기상청 날씨누리에서 확인하세요."))); return; }
    const want = (name) => state.type === "all" || typeOf(name) === state.type;
    const groups = new Map();
    const add = (label, cls, code) => { if (!groups.has(label)) groups.set(label, { cls, codes: [] }); groups.get(label).codes.push(code); };
    for (const a of w.active) if (want(a.name) && a.code.startsWith("L")) add(a.name, isWarningLevel(a.name) ? "warn" : "adv", a.code);
    for (const p of w.preliminary) if (want(p.name) && p.code.startsWith("L")) add(`${p.name}${p.expected ? ` (${p.expected})` : ""}`, "pre", p.code);
    const sea = [...w.active, ...w.preliminary].filter((x) => want(x.name) && x.code.startsWith("S")).length;
    if (!groups.size) panel.append(h("p", { class: "empty", text: state.type === "all" ? "지금 전국 육상에 발효 중인 특보·예비특보가 없습니다." : `지금 육상에 ${state.type} 특보·예비특보가 없습니다.` }));
    else {
      const list = h("ul", { class: "plain wlist" });
      for (const [label, group] of groups) {
        const names = [...new Set(group.codes)].map((code) => h("button", { class: "linklike", type: "button", onclick: () => { pick({ kind: "zone", code }); focusZone(code); } }, zoneFullName(code)));
        list.append(h("li", null, h("span", { class: `pill ${group.cls}`, text: group.cls === "pre" ? "예비특보" : "발효" }), " ", h("b", { text: label }), h("span", { class: "count", text: ` ${names.length}곳` }), h("div", { class: "zone-names" }, names.flatMap((n, i) => (i ? [", ", n] : [n])))));
      }
      panel.append(list);
    }
    if (sea) panel.append(h("p", { class: "note", text: `해상 특보·예비특보 ${sea}건은 지도와 목록에 표시하지 않았습니다.` }));
    if ((w.unresolved || []).length) panel.append(h("p", { class: "note", text: `구역을 찾지 못해 지도에 표시하지 못한 특보: ${w.unresolved.map((u) => `${u.name} ${u.zone}`).join(", ")}` }));
    panel.append(h("p", { class: "official-foot" }, `기상청 발표 기준${w.provider ? ` (${w.provider})` : ""}, ${fmtClock(w.generated_at)} 확인 · `, h("a", { href: OFFICIAL_URL, rel: "noopener" }, "날씨누리 특보 현황")));
  }

  function focusZone(code) {
    const zone = state.zones[code];
    if (!zone) return;
    const [lon, lat] = zone.c, w = Math.min(state.view.w, FULL.w / 3);
    setView({ x: X(lon) - w / 2, y: Y(lat) - (w * state.view.h) / state.view.w / 2, w, h: (w * state.view.h) / state.view.w });
    $("map").scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // ------------------------------------------------------------ 열차 지연 통계

  function statRow(label, st, lateKey, strong) {
    if (!st || !st.n) return h("tr", { class: strong ? "strong" : null }, h("th", { scope: "row", text: label }), ["0", "–", "–", "–"].map((c) => h("td", { text: c })));
    return h("tr", { class: strong ? "strong" : null }, h("th", { scope: "row", text: label }),
      h("td", { text: String(st.n) }), h("td", { text: num(st.mean) }), h("td", { text: num(st.median) }),
      h("td", null, share(st[lateKey], st.n), h("small", null, `${st[lateKey]}편`)));
  }

  function renderDelay() {
    const box = clear($("delay")), period = $("delay-period"), sum = state.summary;
    if (!sum || !sum.period || !sum.period.days) {
      period.textContent = "";
      box.append(h("div", { class: "panel" }, h("p", { class: "empty", text: "아직 집계된 열차 운행 기록이 없습니다. 수집이 시작되면 표시됩니다." })));
      return;
    }
    const late = (sum.late_minutes || [5, 10, 20])[1] || 10, lateKey = `late${late}`;
    period.textContent = `${fmtDate(sum.period.from)} ~ ${fmtDate(sum.period.to)} (${sum.period.days}일) · 코레일 공식 운행 기록 기준${sum.demo ? " · 데모 자료" : ""}`;
    const day = sum.latest_day, last = sum.daily[sum.daily.length - 1] || {};

    const tile = (label, value, unit, sub) => h("div", { class: "tile" }, h("p", { class: "label", text: label }), h("p", { class: "value" }, value, unit ? h("small", null, unit) : null), h("p", { class: "sub", text: sub }));
    box.append(h("h3", { class: "sub-h", text: `${fmtDate(day.date)} 하루` }), h("div", { class: "tiles" },
      tile("지연을 계산한 열차", String(day.all.n || 0), "편", `운행 기록 ${day.counts.recorded}편 중`),
      tile("평균 종착 지연", num(day.all.mean), "분", `가장 늦은 열차 ${day.all.max ?? "–"}분`),
      tile(`${late}분 이상 지연`, String(day.all[lateKey] ?? 0), "편", `전체의 ${share(day.all[lateKey] || 0, day.all.n || 0)}`),
      tile("특보 구간을 지난 열차", String(last.warned ?? 0), "편", Object.keys(day.by_warning).length ? Object.keys(day.by_warning).join("·") : "해당 없음")));

    const compare = h("tbody", null,
      statRow("특보 구간을 지나지 않음", sum.clear, lateKey, true),
      statRow("특보 구간을 지남", sum.warned, lateKey, true),
      Object.entries(sum.by_warning).map(([name, st]) => statRow(`· ${name}`, st, lateKey)));
    box.append(h("h3", { class: "sub-h", text: `특보 구간을 지난 열차는 얼마나 늦었나 (누적 ${sum.period.days}일)` }),
      h("div", { class: "panel" },
        h("div", { class: "table-scroll" }, h("table", { class: "fit" },
          h("thead", null, h("tr", null, h("th", { scope: "col", text: "열차" }), ["열차 수", "평균(분)", "중앙값(분)", `${late}분 이상`].map((t) => h("th", { scope: "col", text: t })))), compare)),
        (sum.warned.n || 0) < 30 ? h("p", { class: "note", text: `특보 구간을 지난 열차가 아직 ${sum.warned.n || 0}편뿐이라 차이를 단정하기 어렵습니다. 자료가 쌓이면 달라질 수 있습니다.` }) : null,
        h("p", { class: "note", text: "특보와 지연이 같은 때 있었다는 뜻이며, 특보가 지연의 원인이라는 뜻은 아닙니다." })));

    box.append(h("h3", { class: "sub-h", text: "최근 날짜별 평균 지연" }), h("div", { class: "panel" }, trendChart(sum.daily, lateKey, late)));

    const lines = Object.entries(sum.by_line).map(([name, v]) => ({ name, warn: v.warn, clear: v.clear, n: (v.warn.n || 0) + (v.clear.n || 0) })).sort((a, b) => b.n - a.n);
    const lineRow = (l) => h("tr", null, h("th", { scope: "row", text: l.name }), h("td", { text: String(l.n) }), h("td", { text: num(l.clear.mean) }), h("td", { text: l.warn.n ? `${num(l.warn.mean)} (${l.warn.n}편)` : "–" }));
    const head = h("thead", null, h("tr", null, h("th", { scope: "col", text: "노선" }), h("th", { scope: "col", text: "열차 수" }), h("th", { scope: "col" }, "평균 지연(분)", h("small", null, "특보 없을 때")), h("th", { scope: "col" }, "평균 지연(분)", h("small", null, "특보 구간 통과"))));
    box.append(h("h3", { class: "sub-h", text: `노선별 (누적 ${sum.period.days}일)` }), h("div", { class: "panel" },
      h("div", { class: "table-scroll" }, h("table", null, head, h("tbody", null, lines.slice(0, 10).map(lineRow)))),
      lines.length > 10 ? h("details", { class: "data-table" }, h("summary", { text: `나머지 ${lines.length - 10}개 노선 보기` }), h("div", { class: "table-scroll" }, h("table", null, h("tbody", null, lines.slice(10).map(lineRow))))) : null,
      h("p", { class: "note", text: "노선은 열차의 주운행선 기준입니다(예: 전라선 KTX는 경부·호남고속선 구간을 달려도 전라선으로 셉니다)." })));

    if (day.top_delayed && day.top_delayed.length) {
      box.append(h("h3", { class: "sub-h", text: `${fmtDate(day.date)} 종착 지연이 컸던 열차` }), h("div", { class: "panel" }, h("div", { class: "table-scroll" }, h("table", { class: "fit" },
        h("thead", null, h("tr", null, ["열차 (계획 도착 시각)", "지연(분)", "지나간 특보"].map((t) => h("th", { scope: "col", text: t })))),
        h("tbody", null, day.top_delayed.slice(0, 10).map((t) => h("tr", null,
          h("th", { scope: "row" }, `${t.class} ${Number(t.trn_no)}`, h("small", null, `${stationLabel(t.origin)} → ${stationLabel(t.dest)}${t.plan_arr ? ` · ${t.plan_arr} 도착` : ""}`)),
          h("td", { class: "over", text: String(t.arr_delay) }), h("td", { text: (t.warnings || []).join(", ") || "–" }))))))));
    }
  }

  /** 날짜별 평균 지연 막대 그래프 (계열 하나). 값은 막대를 가리키거나 아래 표로도 볼 수 있다. */
  function trendChart(daily, lateKey, late) {
    const wrap = h("figure", { style: "margin:0" });
    const chart = h("div", { class: "chart" });
    wrap.append(chart);
    const W = Math.max(300, Math.min(720, ($("delay").clientWidth || 640) - 34)), H = 210, M = { top: 26, right: 8, bottom: 30, left: 30 };
    const max = Math.max(5, ...daily.map((d) => d.mean || 0));
    const step = max <= 6 ? 2 : max <= 15 ? 5 : 10, top = Math.ceil(max / step) * step;
    const plotW = W - M.left - M.right, plotH = H - M.top - M.bottom, band = plotW / daily.length, bar = Math.min(24, band * 0.62);
    const y = (v) => M.top + plotH * (1 - v / top);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `최근 ${daily.length}일의 날짜별 평균 종착 지연(분) 막대 그래프` });
    const grid = s("g", { class: "grid" });
    for (let v = 0; v <= top; v += step) { grid.append(s("line", { x1: M.left, x2: W - M.right, y1: y(v), y2: y(v) })); svg.append(s("text", { class: "tick", x: M.left - 6, y: y(v) + 4, "text-anchor": "end" }, String(v))); }
    svg.prepend(grid);
    svg.append(s("text", { class: "tick", x: M.left - 6, y: 11, "text-anchor": "end" }, "분"));
    const tip = h("div", { class: "tip", hidden: true });
    daily.forEach((d, i) => {
      const cx = M.left + band * (i + 0.5), value = d.mean || 0, y0 = y(0), y1 = y(value), r = Math.min(4, (y0 - y1) / 2);
      const path = value > 0 ? `M${cx - bar / 2} ${y0}V${y1 + r}Q${cx - bar / 2} ${y1} ${cx - bar / 2 + r} ${y1}H${cx + bar / 2 - r}Q${cx + bar / 2} ${y1} ${cx + bar / 2} ${y1 + r}V${y0}Z` : "";
      const [, m, dd] = d.date.split("-");
      const label = `${Number(m)}월 ${Number(dd)}일: 평균 ${num(d.mean)}분, ${late}분 이상 ${d[lateKey] ?? 0}편, 특보 구간 통과 ${d.warned || 0}편, 열차 ${d.n || 0}편`;
      const show = () => {
        clear(tip).append(h("p", { class: "tip-time", text: `${Number(m)}월 ${Number(dd)}일` }),
          h("div", { class: "tip-row" }, h("span", null, "평균 지연"), h("b", null, `${num(d.mean)}분`)),
          h("div", { class: "tip-row" }, h("span", null, `${late}분 이상`), h("b", null, `${d[lateKey] ?? 0}편`)),
          h("div", { class: "tip-row" }, h("span", null, "특보 구간 통과"), h("b", null, `${d.warned || 0}편`)));
        tip.hidden = false;
        const scale = svg.getBoundingClientRect().width / W, px = cx * scale, width = tip.offsetWidth || 150;
        tip.style.left = `${Math.max(0, Math.min(px - width / 2, W * scale - width))}px`;
        tip.style.top = "0px";
      };
      const g = s("g", { class: "bar-g", tabindex: "0", role: "img", "aria-label": label, onpointerenter: show, onfocus: show, onpointerleave: () => { tip.hidden = true; }, onblur: () => { tip.hidden = true; } },
        s("rect", { x: cx - band / 2, y: M.top, width: band, height: plotH, fill: "transparent" }),
        path ? s("path", { class: "bar", d: path }) : null,
        d.warned ? s("path", { class: "warn-mark", d: `M${cx} ${y1 - 12}l4 4l-4 4l-4 -4Z` }) : null);
      svg.append(g);
      // 좁으면 하루 건너 하나씩 적되, 마지막 날(가장 최근)은 늘 적는다
      if (band >= 30 || (daily.length - 1 - i) % 2 === 0) svg.append(s("text", { class: "tick", x: cx, y: H - 12, "text-anchor": "middle" }, `${Number(dd)}일`));
    });
    svg.append(s("line", { class: "axis-line", x1: M.left, x2: W - M.right, y1: y(0), y2: y(0) }));
    chart.append(svg, tip);
    wrap.append(h("ul", { class: "legend" },
      h("li", null, h("span", { class: "swatch bar-key" }), "평균 종착 지연(분)"),
      h("li", null, h("span", { class: "swatch warn-key" }), "특보 구간을 지난 열차가 있던 날")));
    wrap.append(h("details", { class: "data-table" }, h("summary", { text: "표로 보기" }), h("div", { class: "table-scroll" }, h("table", null,
      h("thead", null, h("tr", null, ["날짜", "열차 수", "평균(분)", `${late}분 이상`, "최대(분)", "특보 구간 통과"].map((t) => h("th", { scope: "col", text: t })))),
      h("tbody", null, daily.map((d) => h("tr", null, h("th", { scope: "row", text: fmtDate(d.date) }), [d.n, num(d.mean), d[lateKey], d.max, d.warned].map((v) => h("td", { text: v == null ? "–" : String(v) })))))))));
    return wrap;
  }

  // ------------------------------------------------------------ 시작

  function renderAll() {
    renderHead();
    renderTypeChips();
    renderLegend();
    paintZones();
    renderPicked();
    renderList();
    renderDelay();
  }

  async function loadData() {
    const [warnings, latest, summary] = await Promise.all([
      getJson("data/warnings.json", true).catch(() => null),
      getJson("data/latest.json", true).catch(() => null),
      getJson("data/rail.json", true).catch(() => null),
    ]);
    state.warnings = warnings || state.warnings || { available: false, active: [], preliminary: [], unresolved: [] };
    state.latest = latest || state.latest;
    state.summary = summary || state.summary;
    renderAll();
  }

  async function start() {
    try {
      [state.zones, state.rail, state.stations] = await Promise.all([getJson("geo/zones.json"), getJson("geo/rail.json"), getJson("geo/stations.json")]);
    } catch (error) {
      $("updated").textContent = "지도 자료를 불러오지 못했습니다";
      clear($("map")).append(h("p", { class: "empty", text: "지도 자료를 불러오지 못했습니다. 잠시 뒤 새로 고쳐 주세요." }));
      return;
    }
    drawMap();
    $("zoom-in").addEventListener("click", () => zoomBy(1.6));
    $("zoom-out").addEventListener("click", () => zoomBy(1 / 1.6));
    $("zoom-all").addEventListener("click", () => setView({ ...FULL }));
    $("zoom-jn").addEventListener("click", () => setView({ ...JEONNAM }));
    await loadData();
    let resizeTimer = 0, lastWidth = $("delay").clientWidth;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { if ($("delay").clientWidth !== lastWidth) { lastWidth = $("delay").clientWidth; renderDelay(); } }, 200);
    });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) loadData(); });
    setInterval(() => { if (!document.hidden) loadData(); }, REFRESH_MS);
  }

  start();
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => { /* 오프라인 지원 없이 계속 */ }));
  }
})();
