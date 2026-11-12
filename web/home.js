/* 첫 화면 — 지역을 찾으면 그 지역의 특보 가능성만 보여 준다. 지도에서 눌러도 된다.
 *
 * 자료
 *  data/latest.json   3시간마다 계산한 지역별 가능성(등급·%·예상 시간대)
 *  live.json          몇 분마다 확인한 공식 특보·예비특보(있으면 덮어 쓴다: 발효 중이면 '발효 중', 예비특보면 '높음' 이상)
 *  geo/zones.json     특보구역 경계(지도)
 * 원칙: 받아 온 글은 늘 텍스트로 넣는다. 공식 특보가 있으면 가능성보다 먼저 보여 준다.
 */
(() => {
  "use strict";
  const { h, s, $, clear, fmtClock, fmtAgo, getJson } = window.TeukboUI;
  const Live = window.TeukboLive, P = Live.radarProject;
  const GRADE = { low: "낮음", caution: "주의", high: "높음", active: "발효 중", unknown: "확인 불가" };
  const RANK = { unknown: -1, low: 0, caution: 1, high: 2, active: 3 };
  const WEEKDAY = ["일", "월", "화", "수", "목", "금", "토"];
  const RELOAD_MS = 10 * 60 * 1000, STORE_KEY = "teukbo.region", MAX_MATCHES = 8, MAX_OTHERS = 4;
  const state = { base: null, latest: null, live: null, zones: null, region: null, query: "", paths: new Map(), liveTimer: null };

  const put = (node, ...kids) => { node.append(...kids.flat(Infinity).filter((kid) => kid != null && kid !== false)); return node; };
  const plain = (text) => String(text || "").replace(/[\s()·.]/g, "").toLowerCase();
  const shortName = (name) => String(name).replace(/\(.*\)/, "").replace(/(특별자치시|광역시|특별시|시|군)$/, "") || String(name);

  /** 한국 시각으로 '13일(금) 19시' — 보는 기기의 시간대와 상관없이 같게 */
  function dayHour(iso) {
    const t = new Date(new Date(iso).getTime() + 9 * 3600 * 1000);
    return { day: `${t.getUTCDate()}일(${WEEKDAY[t.getUTCDay()]})`, hour: `${String(t.getUTCHours()).padStart(2, "0")}시` };
  }
  function rangeText(from, to) {
    const a = dayHour(from), b = dayHour(to || from);
    if (!to || to === from) return `${a.day} ${a.hour} 무렵`;
    return a.day === b.day ? `${a.day} ${a.hour} ~ ${b.hour}` : `${a.day} ${a.hour} ~ ${b.day} ${b.hour}`;
  }
  const hazardName = (id) => (state.latest.hazards.find((x) => x.id === id) || {}).name || id;
  const regionOf = (id) => state.latest.regions.find((r) => r.id === id) || null;

  // ------------------------------------------------------------ 지역 찾기

  function matches() {
    const q = plain(state.query);
    return q ? state.latest.regions.filter((r) => plain(`${r.name}${r.province || ""}`).includes(q)) : [];
  }

  function renderMatches() {
    const box = clear($("matches")), found = matches();
    if (!plain(state.query)) return;
    if (!found.length) {
      const scope = [...new Set(state.latest.regions.map((r) => r.province).filter(Boolean))].join("·");
      box.append(h("p", { class: "finder-none", text: `‘${state.query.trim()}’은(는) 아직 계산하지 않는 지역입니다. 지금은 ${scope ? `${scope} ` : ""}${state.latest.regions.length}개 지역만 계산합니다.` }));
      return;
    }
    put(box, found.slice(0, MAX_MATCHES).map((r) => h("button", { class: "chip", type: "button", "aria-pressed": String(r.id === state.region), onclick: () => pick(r.id, true) }, r.name)),
      found.length > MAX_MATCHES ? h("span", { class: "hint", text: `외 ${found.length - MAX_MATCHES}곳 — 이름을 더 입력하세요` }) : null);
  }

  function pick(id, clearQuery, fromMap) {
    if (!regionOf(id)) return;
    state.region = id;
    try { localStorage.setItem(STORE_KEY, id); } catch (_) { /* 저장소를 못 써도 화면은 동작한다 */ }
    if (clearQuery) { state.query = ""; $("q").value = ""; }
    render();
    // 지도에서 골랐는데 가능성 칸이 화면 위로 밀려나 있으면 보이게 올려 준다
    if (fromMap && $("result").getBoundingClientRect().top < 0) $("result").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ------------------------------------------------------------ 가능성 (고른 지역)

  /** 그 지역에서 보여 줄 특보 종류: 발효 중·예비특보이거나 가능성이 0보다 큰 것. 등급 높은 순, 같으면 % 높은 순. */
  function rowsOf(region) {
    return state.latest.hazards.map((hazard) => ({ hazard, sum: region.hazards[hazard.id] }))
      .filter((x) => x.sum && (x.sum.g === "active" || x.sum.official || (x.sum.p || 0) > 0))
      .sort((a, b) => RANK[b.sum.g] - RANK[a.sum.g] || (b.sum.p || 0) - (a.sum.p || 0));
  }

  const gradePill = (grade) => h("span", { class: `pill g-${grade}`, text: GRADE[grade] || GRADE.unknown });
  const percent = (sum) => (sum.g === "active" ? "발효 중" : sum.p == null ? "–" : `${sum.p}%`);

  function renderResult() {
    const box = clear($("result")), region = regionOf(state.region);
    if (!region) return;
    const rows = rowsOf(region), top = rows[0], official = region.official || {};
    const card = h("div", { class: `card result g-${top ? top.sum.g : "low"}` });
    card.append(h("p", { class: "where" }, h("b", null, region.name), region.province ? h("small", null, region.province) : null));

    if (!top) {
      put(card, h("p", { class: "headline" }, h("span", { class: "big" }, "낮음")),
        h("p", { class: "when", text: `앞으로 ${state.latest.horizon_hours}시간, 모든 특보 종류의 가능성이 0%입니다.` }));
    } else {
      const sum = top.sum, active = sum.g === "active";
      put(card, h("p", { class: "hz", text: top.hazard.name }),
        h("p", { class: "headline" }, h("span", { class: active ? "big word" : "big" }, active ? "발효 중" : sum.p == null ? "–" : String(sum.p), !active && sum.p != null ? h("small", null, "%") : null), active ? null : gradePill(sum.g)),
        active ? (sum.official_from ? h("p", { class: "when", text: `${rangeText(sum.official_from)} 발효` }) : null)
          : sum.from ? h("p", { class: "when", text: `${rangeText(sum.from, sum.to)} 기준 도달 예상` }) : null);
    }

    // 공식 특보·예비특보(있으면 가능성보다 이것이 우선이다)
    const lines = [];
    for (const item of official.active || []) lines.push(h("li", null, h("span", { class: "pill g-active", text: "발효 중" }), ` ${item.name}`));
    for (const item of official.preliminary || []) lines.push(h("li", null, h("span", { class: "pill g-pre", text: "예비특보" }), ` ${item.name}${item.expected ? ` · ${item.expected}` : ""}`));
    if (lines.length) card.append(h("ul", { class: "official-lines", "aria-label": "기상청 공식 특보" }, lines));
    else if (official.available === false) card.append(h("p", { class: "basis", text: "기상청 공식 특보는 지금 확인하지 못했습니다." }));

    const others = rows.slice(1);
    if (others.length) {
      put(card, h("ul", { class: "others", "aria-label": "다른 특보 종류" }, others.slice(0, MAX_OTHERS).map((x) => h("li", null, h("span", { class: "name", text: x.hazard.name }), h("b", null, percent(x.sum)), x.sum.g === "active" ? null : gradePill(x.sum.g)))),
        others.length > MAX_OTHERS ? h("p", { class: "basis", text: `외 ${others.length - MAX_OTHERS}종` }) : null);
    }
    const models = (region.models || []).length;
    card.append(h("p", { class: "basis", text: `앞으로 ${state.latest.horizon_hours}시간${models ? ` · 예보 ${models}개 비교` : ""} · 참고값` }));
    box.append(card);
  }

  // ------------------------------------------------------------ 지도

  const pathOf = (rings) => rings.map((ring) => { let d = ""; for (let i = 0; i < ring.length; i += 2) { const [x, y] = P(ring[i], ring[i + 1]); d += `${i ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`; } return `${d}Z`; }).join("");

  /** 지도는 한 번만 그린다(경계는 바뀌지 않는다). 색과 고른 표시는 paintMap 이 바꾼다. */
  function drawMap() {
    const holder = clear($("map")), zones = state.zones;
    state.paths.clear();
    const owner = new Map();  // 특보구역 코드 → 그 구역을 쓰는 첫 지역
    for (const region of state.latest.regions) for (const code of region.zones || []) if (!owner.has(code) && zones[code]) owner.set(code, region.id);
    if (!owner.size) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const code of owner.keys()) for (const ring of zones[code].p) for (let i = 0; i < ring.length; i += 2) {
      const [x, y] = P(ring[i], ring[i + 1]);
      x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
    }
    const pad = 3, box = [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad];
    const svg = s("svg", { viewBox: box.map((v) => v.toFixed(1)).join(" "), role: "group", "aria-label": "지역별 특보 가능성 지도. 지역을 누르면 그 지역의 가능성을 보여 줍니다." });
    const around = s("g", { class: "around", "aria-hidden": "true" }), covered = s("g", { class: "covered" }), labels = s("g", { class: "labels", "aria-hidden": "true" });
    for (const [code, zone] of Object.entries(zones)) {
      if (owner.has(code)) continue;
      const [cx, cy] = P(zone.c[0], zone.c[1]);
      if (cx < box[0] - 25 || cx > box[0] + box[2] + 25 || cy < box[1] - 25 || cy > box[1] + box[3] + 25) continue;  // 둘레의 이웃 구역만
      around.append(s("path", { d: pathOf(zone.p) }));
    }
    const labelled = new Set();
    for (const [code, id] of owner) {
      const zone = zones[code], region = regionOf(id);
      const path = s("path", { d: pathOf(zone.p), "data-region": id, tabindex: "0", role: "button", onclick: () => pick(id, true, true),
        onkeydown: (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pick(id, true, true); } } });
      covered.append(path);
      if (!state.paths.has(id)) state.paths.set(id, []);
      state.paths.get(id).push(path);
      if (!labelled.has(id)) {  // 구역이 여럿인 지역은 첫 구역에만 이름을 적는다
        labelled.add(id);
        const [cx, cy] = P(zone.c[0], zone.c[1]);
        labels.append(s("text", { x: cx.toFixed(1), y: (cy + 1).toFixed(1), "text-anchor": "middle" }, shortName(region.name)));
      }
    }
    svg.append(around, covered, labels);
    holder.append(svg);
    put(clear($("legend")), ["low", "caution", "high", "active"].map((grade) => h("li", null, h("span", { class: `swatch map-key g-${grade}` }), GRADE[grade])));
  }

  function paintMap() {
    for (const [id, paths] of state.paths) {
      const region = regionOf(id), grade = region && region.top ? region.top.grade : "low", picked = id === state.region;
      const top = region && region.top;
      const label = region ? `${region.name}: ${top && RANK[top.grade] > 0 ? `${hazardName(top.hazard)} ${top.grade === "active" ? "발효 중" : `${top.p == null ? "" : `${top.p}% `}${GRADE[top.grade]}`}` : "가능성 낮음"}` : id;
      for (const path of paths) {
        path.setAttribute("class", `zone g-${grade}${picked ? " picked" : ""}`);
        path.setAttribute("aria-label", label);
        path.setAttribute("aria-pressed", String(picked));
        if (picked) path.parentNode.append(path);  // 고른 지역의 테두리가 이웃에 가리지 않게 맨 위로
      }
    }
  }

  // ------------------------------------------------------------ 머리말 · 전체

  function renderHead() {
    const base = state.base, alerts = clear($("alerts"));
    const liveCfg = base.live || {}, usable = state.live && Live.fresh(state.live.warnings, liveCfg.stale_after_minutes || 30);
    const checked = usable ? fmtClock(state.live.warnings.checked_at).split(" ").pop() : null;
    $("updated").textContent = `계산 ${fmtClock(base.generated_at)} · ${fmtAgo(base.generated_at)}${checked ? ` · 특보 확인 ${checked}` : ""}`;
    if (base.demo) alerts.append(h("div", { class: "banner demo", role: "status" }, h("div", null, "데모 화면입니다. 표시된 수치는 실제 예보가 아닌 가상 자료입니다.")));
    const hours = (Date.now() - new Date(base.generated_at).getTime()) / 3600000;
    if (hours > (base.stale_after_hours || 7)) alerts.append(h("div", { class: "banner", role: "status" }, h("div", null, `자료가 ${Math.floor(hours)}시간 전 것입니다. 최신 예보가 반영되지 않았을 수 있습니다.`)));
  }

  function render() {
    if (!state.latest) return;
    if (!regionOf(state.region)) state.region = state.latest.regions[0] ? state.latest.regions[0].id : null;
    renderHead(); renderMatches(); renderResult(); paintMap();
  }

  function applyLive() {
    state.latest = Live.applyToLatest(state.base, state.live, (state.base.live || {}).stale_after_minutes || 30);
    render();
  }

  async function refreshLive() {
    const config = state.base && state.base.live;
    if (!config || !config.url) return;
    try { state.live = (await Live.load(config)) || state.live; } catch (_) { /* 실시간 자료를 못 받아도 3시간 자료로 보여 준다 */ }
    applyLive();
  }

  async function load() {
    try {
      const [latest, zones] = await Promise.all([getJson("data/latest.json"), state.zones || getJson("geo/zones.json")]);
      const first = !state.base;
      state.base = latest; state.zones = zones;
      state.latest = Live.applyToLatest(latest, state.live, (latest.live || {}).stale_after_minutes || 30);
      if (first) {
        try { state.region = localStorage.getItem(STORE_KEY); } catch (_) { /* 무시 */ }
        if (!regionOf(state.region)) state.region = null;
      }
      drawMap();
      render();
      refreshLive();
      const poll = Math.max(30, Number((latest.live || {}).poll_seconds) || 120) * 1000;
      if (!state.liveTimer && latest.live && latest.live.url) state.liveTimer = setInterval(refreshLive, poll);
    } catch (error) {
      if (!state.base) {
        $("updated").textContent = "";
        clear($("result")).append(h("div", { class: "panel" }, h("p", { class: "empty", text: "자료를 불러오지 못했습니다. 잠시 뒤 다시 열어 주세요." })));
      }
    }
  }

  $("q").addEventListener("input", (event) => { state.query = event.target.value; if (state.latest) renderMatches(); });
  $("q").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !state.latest) return;
    const found = matches();
    if (found.length) { event.preventDefault(); pick(found[0].id, true); event.target.blur(); }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.base) { load(); } });
  setInterval(load, RELOAD_MS);
  load();
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => { /* 오프라인 지원 없이 계속 */ }));
  }
})();
