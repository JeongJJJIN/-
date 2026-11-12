/* 기상특보 가능성 — 화면.
 * data/latest.json(전체 지역 요약)을 읽고, 지역을 고르면 data/detail/<지역>.json(모델 비교 자료)을 읽는다.
 * 계산은 모두 서버(GitHub Actions)에서 끝나 있고 여기서는 보여 주기만 한다.
 * 특보·예비특보와 지금 날씨는 몇 분마다 새로 만들어지는 실시간 자료(live.js)를 덮어 보여 준다.
 */
(() => {
  "use strict";

  const GRADE_LABEL = { low: "낮음", caution: "주의", high: "높음", active: "발효 중", unknown: "자료 없음" };
  const GRADE_RANK = { unknown: -1, low: 0, caution: 1, high: 2, active: 3 };
  const OFFICIAL_URL = "https://www.weather.go.kr/w/special-report/overall.do";
  const STANDARD_URL = "https://www.weather.go.kr/w/forecast/guide/standard.do";
  const VARIABLE_LABEL = { temp: "시간별 기온", apparent: "시간별 체감온도" };
  const REFRESH_MS = 10 * 60 * 1000;
  const SVG_NS = "http://www.w3.org/2000/svg";

  const Live = window.TeukboLive;
  // base = 받아 온 latest.json 그대로, latest = 그 위에 실시간 특보를 덮은 것(화면은 이것을 그린다)
  const state = { base: null, latest: null, live: null, liveFailed: false, liveSig: "", geo: null, details: {}, regionId: null, chartPick: {}, open: new Set(), showQuiet: false };

  // ------------------------------------------------------------ DOM 도우미

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

  // ------------------------------------------------------------ 시각 (항상 한국시간으로 표시)

  const KST = new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul", month: "numeric", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  function kst(iso) {
    const out = {};
    for (const part of KST.formatToParts(new Date(iso))) out[part.type] = part.value;
    return { month: Number(out.month), day: Number(out.day), weekday: out.weekday, hour: Number(out.hour), minute: out.minute };
  }
  const two = (n) => String(n).padStart(2, "0");
  const fmtDay = (iso) => { const p = kst(iso); return `${p.day}일(${p.weekday})`; };
  const fmtDayHour = (iso) => { const p = kst(iso); return `${p.day}일(${p.weekday}) ${two(p.hour)}시`; };
  const fmtClock = (iso) => { const p = kst(iso); return `${p.month}월 ${p.day}일 ${two(p.hour)}:${p.minute}`; };
  function fmtRange(from, to) {
    if (!to || from === to) return `${fmtDayHour(from)}경`;
    const a = kst(from), b = kst(to);
    if (a.day === b.day && a.month === b.month) return `${a.day}일(${a.weekday}) ${two(a.hour)}~${two(b.hour)}시`;
    return `${fmtDayHour(from)} ~ ${fmtDayHour(to)}`;
  }
  function fmtAgo(iso) {
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 1) return "방금";
    if (minutes < 60) return `${minutes}분 전`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours}시간 ${minutes % 60 ? `${minutes % 60}분 ` : ""}전`;
    return `${Math.floor(hours / 24)}일 전`;
  }
  const fmtNum = (value, decimals = 1) => (value == null ? "–" : Number(value).toFixed(decimals).replace(/\.0+$/, ""));
  const pct = (ratio) => `${Math.round(ratio * 100)}%`;

  // ------------------------------------------------------------ 자료 읽기

  async function getJson(path) {
    const response = await fetch(path, { cache: "no-cache" });
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    return response.json();
  }

  async function loadLatest() {
    const main = $("main");
    main.classList.add("loading");
    try {
      const latest = await getJson("data/latest.json");
      const changed = !state.base || state.base.generated_at !== latest.generated_at;
      state.base = latest;
      state.latest = Live.applyToLatest(latest, state.live, liveStale());
      if (changed) state.details = {};
      if (changed || !$("region").options.length) renderAll();
      else renderUpdated();
      if (changed) loadLive();
    } catch (error) {
      if (!state.latest) renderLoadError(error);
    } finally {
      main.classList.remove("loading");
    }
  }

  const liveConfig = () => (state.base && state.base.live) || null;
  const liveStale = () => (liveConfig() || {}).stale_after_minutes || 30;

  /** 실시간 자료를 다시 읽는다. 특보가 달라졌으면 전체를, 아니면 실시간 부분만 다시 그린다. */
  async function loadLive() {
    if (!liveConfig()) return;
    try {
      state.live = (await Live.load(liveConfig())) || state.live;
      state.liveFailed = false;
    } catch (_) {
      state.liveFailed = true;  // 직전에 받은 값은 그대로 두되, 오래되면 쓰지 않는다(확인 시각으로 판단)
    }
    refreshLive();
  }

  function refreshLive() {
    if (!state.base) return;
    const usable = state.live && Live.fresh(state.live.warnings, liveStale());
    const sig = usable ? Live.warningsSignature(state.live) : "(3시간 자료)";
    state.latest = Live.applyToLatest(state.base, state.live, liveStale());
    if (sig !== state.liveSig) {
      state.liveSig = sig;
      renderAlerts();
      renderOverview();
      renderRegion();
      renderRailOutlook();
    } else {
      renderOfficial();
      renderNow();
    }
    renderUpdated();
  }

  function loadDetail(regionId) {
    if (!state.details[regionId]) {
      state.details[regionId] = getJson(`data/detail/${encodeURIComponent(regionId)}.json`).catch((error) => {
        delete state.details[regionId];
        throw error;
      });
    }
    return state.details[regionId];
  }

  // ------------------------------------------------------------ 공통 조각

  function gradeMark(grade) {
    const svg = s("svg", { class: "mark", viewBox: "0 0 10 10", "aria-hidden": "true" });
    if (grade === "caution") svg.append(s("path", { class: "mark-caution", d: "M5 1 9.4 9H.6Z" }));
    else if (grade === "high") svg.append(s("path", { class: "mark-high", d: "M5 .6 9.4 5 5 9.4.6 5Z" }));
    else if (grade === "active") svg.append(s("circle", { class: "mark-active", cx: 5, cy: 5, r: 4.4 }));
    else if (grade === "unknown") svg.append(s("circle", { class: "mark-unknown", cx: 5, cy: 5, r: 3.6 }));
    else svg.append(s("circle", { class: "mark-low", cx: 5, cy: 5, r: 3.6 }));
    return svg;
  }
  function badge(grade, label) {
    return h("span", { class: "badge", "data-grade": grade }, gradeMark(grade), label || GRADE_LABEL[grade] || grade);
  }
  const isFuture = (iso) => Boolean(iso) && new Date(iso).getTime() > Date.now();
  /** 특보구역 이름이 지역 이름과 다를 때만(세분 구역·도서·도 전체 등) 보여 준다. */
  const zoneNote = (zone, name) => (zone && ![name, name.replace(/[시군]$/, "")].includes(zone.replace(/[시군]$/, "")) && zone !== name ? `구역: ${zone}` : null);
  const modelIndex = (id) => state.latest.models.findIndex((m) => m.id === id);
  const modelColor = (id) => `var(--series-${(modelIndex(id) % 8) + 1})`;
  const modelName = (id, short) => { const m = state.latest.models.find((x) => x.id === id); return m ? (short ? m.short : m.name) : id; };
  const region = () => state.latest.regions.find((r) => r.id === state.regionId) || state.latest.regions[0];
  const levelName = (level) => (state.latest.levels[level] || {}).name || level;
  const levelRank = (level) => (state.latest.levels[level] || {}).rank || 0;

  // ------------------------------------------------------------ 머리·알림

  function renderUpdated() {
    const latest = state.latest;
    $("updated").textContent = `가능성 계산 ${fmtClock(latest.generated_at)} · ${fmtAgo(latest.generated_at)}`;
    renderLiveStatus();
  }

  /** 실시간 자료의 상태를 한 줄로: 언제 확인했는지, 늦어지고 있으면 그 사실을. */
  function renderLiveStatus() {
    const box = $("live-status");
    if (!liveConfig()) { box.hidden = true; return; }
    box.hidden = false;
    const w = state.live && state.live.warnings;
    const age = Live.ageMinutes(w);
    if (w && Live.fresh(w, liveStale())) {
      box.className = "live-status ok";
      box.textContent = `특보 실시간 확인 ${fmtClock(w.checked_at).replace(/^.*일 /, "")} · ${fmtAgo(w.checked_at)}`;
    } else {
      box.className = "live-status late";
      box.textContent = Number.isFinite(age)
        ? `실시간 갱신 지연 — 특보 마지막 확인 ${fmtAgo(w.checked_at)}. 아래 특보는 가능성 계산 시각 기준입니다.`
        : "실시간 자료를 아직 받지 못했습니다. 아래 특보는 가능성 계산 시각 기준입니다.";
    }
  }

  function renderAlerts() {
    const latest = state.latest, box = clear($("alerts")), src = latest.sources || {};
    const add = (kind, ...content) => box.append(h("div", { class: `banner ${kind}`, role: "status" }, h("div", null, ...content)));
    if (latest.demo) add("demo", "데모 화면입니다. 표시된 수치는 실제 예보가 아닌 가상 자료입니다.");
    const ageHours = (Date.now() - new Date(latest.generated_at).getTime()) / 3600000;
    if (!latest.demo && ageHours > (latest.stale_after_hours || 7)) {
      add("", h("strong", null, `자료가 ${fmtAgo(latest.generated_at)} 것입니다. `), "갱신이 늦어지고 있어 현재 상황과 다를 수 있습니다. ", h("a", { href: OFFICIAL_URL, rel: "noopener" }, "기상청 날씨누리"), "에서 확인하세요.");
    }
    const warn = src.kma_warnings || {};
    if (!warn.ok) {
      add("", h("strong", null, "기상청 특보 현황을 불러오지 못했습니다. "), warn.configured === false ? "(인증키 미설정) " : "", "발효 중인 특보·예비특보가 반영되지 않았습니다. ", h("a", { href: OFFICIAL_URL, rel: "noopener" }, "기상청 날씨누리"), "에서 꼭 확인하세요.");
    }
    const working = latest.models.filter((m) => m.ok).length;
    if (working < latest.models.length) {
      const missing = latest.models.filter((m) => !m.ok).map((m) => m.short).join(", ");
      add("info", `예측 자료 ${latest.models.length}개 중 ${working}개만 받아 계산했습니다 (빠진 자료: ${missing}).`, working <= 1 ? " 자료가 하나뿐이라 가능성이 0% 또는 100%에 치우칠 수 있습니다." : "");
    }
  }

  function renderLoadError(error) {
    $("updated").textContent = "자료를 불러오지 못했습니다";
    clear($("alerts")).append(
      h("div", { class: "banner", role: "alert" },
        h("div", null, h("strong", null, "최신 자료를 불러오지 못했습니다. "), "네트워크 상태를 확인한 뒤 다시 시도하세요. ",
          h("button", { class: "chip", type: "button", onclick: () => loadLatest() }, "다시 시도"),
          h("p", { class: "note", text: String(error && error.message ? error.message : error) })))
    );
  }

  // ------------------------------------------------------------ 발효 특보·예비특보

  function renderOfficial() {
    const r = region(), box = clear($("official")), official = r.official || {};
    if (!official.available) {
      box.append(h("p", { class: "empty" }, "특보 현황을 확인하지 못했습니다. ", h("a", { href: OFFICIAL_URL, rel: "noopener" }, "기상청 날씨누리에서 확인하세요.")));
      return;
    }
    const items = [];
    for (const w of official.active || []) {
      const upcoming = isFuture(w.from);
      const meta = [w.from ? `${fmtDayHour(w.from)} ${upcoming ? "발효 예정" : "발효"}` : null, zoneNote(w.zone, r.name)].filter(Boolean).join(" · ");
      items.push(h("li", { class: "official-item" }, badge("active", upcoming ? "발효 예정" : "발효 중"), h("span", { class: "name", text: w.name }), meta ? h("span", { class: "meta", text: meta }) : null));
    }
    for (const w of official.preliminary || []) {
      const meta = [w.expected ? `예상 시점: ${w.expected}` : null, zoneNote(w.zone, r.name)].filter(Boolean).join(" · ");
      items.push(h("li", { class: "official-item" }, badge("high", "예비특보"), h("span", { class: "name", text: w.name }), meta ? h("span", { class: "meta", text: meta }) : null));
    }
    if (items.length) box.append(h("ul", { class: "official-list" }, items));
    else box.append(h("p", { class: "empty", text: `${r.name}에 발효 중인 특보·예비특보가 없습니다.` }));
    const provider = (state.latest.sources.kma_warnings || {}).provider;
    const checked = state.latest.warnings_checked_at || state.latest.generated_at;
    box.append(h("p", { class: "official-foot" }, `기상청 발표 기준${provider ? ` (${provider})` : ""}, ${fmtClock(checked)} 확인 · `, h("a", { href: OFFICIAL_URL, rel: "noopener" }, "날씨누리 특보 현황")));
  }

  // ------------------------------------------------------------ 지금 날씨·앞으로 6시간 (실시간 자료)

  function renderNow() {
    const block = $("now-block"), box = clear($("now")), hint = $("now-hint");
    if (!liveConfig()) { block.hidden = true; return; }
    block.hidden = false;
    const live = state.live, r = region(), lr = live && live.regions ? live.regions[r.id] || {} : {};
    const obsOk = live && Live.fresh(live.obs, Math.max(liveStale(), 75)) && lr.now;   // 실황은 한 시간 단위 자료 → 기준을 조금 넉넉히
    const fcstOk = live && Live.fresh(live.fcst, Math.max(liveStale(), 75)) && (lr.fcst || []).length;
    const clock = (iso) => fmtClock(iso).replace(/^.*일 /, "");
    hint.textContent = [obsOk ? `기상청 초단기실황 ${clock(live.obs.base)} 기준(${fmtAgo(live.obs.checked_at)} 확인)` : null,
      fcstOk ? `초단기예보 ${clock(live.fcst.base)} 발표` : null].filter(Boolean).join(" · ");
    if (!obsOk && !fcstOk) {
      box.append(h("div", { class: "panel" }, h("p", { class: "empty" }, `${r.name}의 지금 날씨를 불러오지 못했습니다. `, h("a", { href: "https://www.weather.go.kr/w/weather/now.do", rel: "noopener" }, "기상청 날씨누리에서 확인하세요."))));
      return;
    }
    if (obsOk) {
      const now = lr.now, pty = Live.ptyText(now.pty);
      const tile = (label, value, unit, sub) => h("div", { class: "tile" }, h("p", { class: "label", text: label }), h("p", { class: "value" }, value, unit ? h("small", null, unit) : null), h("p", { class: "sub", text: sub || "\u00a0" }));
      box.append(h("div", { class: "tiles now-tiles" },
        tile("기온", Live.num(now.temp), "℃", now.rh != null ? `습도 ${now.rh}%` : ""),
        tile("1시간 강수량", now.rain == null ? "–" : Live.num(now.rain), "mm", pty || (now.rain === 0 ? "강수 없음" : "")),
        tile("풍속", Live.num(now.wind), "m/s", now.wdir != null ? `${Live.windDir(now.wdir)}풍` : "")));
    }
    if (fcstOk) {
      const rows = lr.fcst.slice(0, 6).map((row) => h("tr", null,
        h("th", { scope: "row", text: `${fmtDayHour(row.t).replace(/^.*\) /, "")}` }),
        h("td", { text: Live.skyText(row) || "–" }),
        h("td", { text: row.temp == null ? "–" : `${Live.num(row.temp)}℃` }),
        h("td", { text: row.pop == null ? "–" : `${row.pop}%` }),
        h("td", { class: row.rain ? "over" : null, text: row.rain || "–" }),
        h("td", { text: row.wind == null ? "–" : `${Live.num(row.wind)}m/s` })));
      box.append(h("div", { class: "panel now-fcst" }, h("div", { class: "table-scroll" }, h("table", { class: "fit now-table" },
        h("thead", null, h("tr", null, [["시각"], ["날씨"], ["기온"], ["강수", "확률"], ["강수량", "1시간"], ["풍속"]].map(([t, sub]) => h("th", { scope: "col" }, t, sub ? h("small", null, sub) : null)))),
        h("tbody", null, rows)))));
    }
    box.append(h("p", { class: "note", text: "시·군청 위치의 격자 값입니다. 같은 시·군 안에서도 산지·해안은 다를 수 있습니다." }));
  }

  // ------------------------------------------------------------ 특보 가능성 카드

  function typeGroups(r) {
    const latest = state.latest;
    const groups = latest.types.map((type, order) => {
      const levels = latest.hazards.filter((hz) => hz.type === type.id).sort((a, b) => levelRank(a.level) - levelRank(b.level))
        .map((hz) => ({ hazard: hz, sum: r.hazards[hz.id] || { g: "unknown", p: null } }));
      const top = levels.reduce((best, cur) => {
        const a = [GRADE_RANK[cur.sum.g], cur.sum.p || 0], b = [GRADE_RANK[best.sum.g], best.sum.p || 0];
        return a[0] > b[0] || (a[0] === b[0] && a[1] > b[1]) ? cur : best;
      }, levels[0]);
      const maxP = Math.max(...levels.map((l) => l.sum.p || 0));
      return { type, order, levels, top, maxP, official: levels.some((l) => l.sum.official) };
    }).filter((g) => g.levels.length);
    groups.sort((a, b) => GRADE_RANK[b.top.sum.g] - GRADE_RANK[a.top.sum.g] || b.maxP - a.maxP || a.order - b.order);
    return groups;
  }
  const isNotable = (g) => GRADE_RANK[g.top.sum.g] >= GRADE_RANK.caution || g.maxP >= 10 || g.official;

  function levelRow(level) {
    const { hazard, sum } = level;
    const sub = [];
    let pctNode;
    if (hazard.method === "official_only") {
      pctNode = h("span", { class: "pct none", text: "–" });
      sub.push(h("span", null, "예측값으로 계산하지 않고 기상청 발표만 반영합니다"));
    } else if (sum.p == null) {
      pctNode = h("span", { class: "pct none", text: "–" });
      sub.push(h("span", null, "판단할 예측 자료가 없습니다"));
    } else {
      pctNode = h("span", { class: "pct" }, String(sum.p), h("small", null, "%"));
      if (sum.off) {
        const months = hazard.months || [];
        sub.push(h("span", null, `적용 기간이 아닙니다${months.length ? ` (${months[0]}월~${months[months.length - 1]}월)` : ""}`));
      } else if (sum.from) {
        sub.push(h("span", { class: "when", text: `${fmtRange(sum.from, sum.to)} 기준 도달 예상` }));
        sub.push(h("span", null, `자료 ${sum.n}개 중 ${sum.x}개가 기준 초과`));
      } else if (sum.reach != null) {
        sub.push(h("span", null, sum.reach >= 0.5 && sum.peak ? `기준 미달 · 최대 ${pct(sum.reach)}까지 접근 (${fmtDayHour(sum.peak)}경)` : `기준에 못 미침 (최대 도달률 ${pct(sum.reach)})`));
      }
    }
    if (sum.official === "active") {
      const upper = sum.official_level && sum.official_level !== hazard.level ? `${levelName(sum.official_level)} ` : "";
      const when = sum.official_from ? ` · ${fmtDayHour(sum.official_from)} ${isFuture(sum.official_from) ? "발효 예정" : "발효"}` : "";
      sub.push(h("span", { class: "tag", text: `기상청 ${upper}발표${when}` }));
    } else if (sum.official === "preliminary") {
      sub.push(h("span", { class: "tag", text: "기상청 예비특보" }));
    }
    const label = sum.g === "active" && isFuture(sum.official_from) ? "발효 예정" : GRADE_LABEL[sum.g];
    const fill = sum.p == null ? 0 : Math.max(sum.p, sum.p > 0 ? 3 : 0);
    return h("div", { class: "level" },
      h("div", { class: "level-top" },
        h("span", { class: "level-name", text: levelName(hazard.level) }),
        h("div", { class: "meter", "data-grade": sum.mg === "unknown" ? "low" : sum.mg, role: "img", "aria-label": sum.p == null ? "가능성 계산 없음" : `가능성 ${sum.p}퍼센트` }, h("span", { style: `width:${fill}%` })),
        pctNode,
        badge(sum.g, label)),
      sub.length ? h("p", { class: "level-sub" }, sub) : null);
  }

  function typeCard(group) {
    const typeId = group.type.id;
    const computed = group.levels.some((l) => l.hazard.method === "model");
    const card = h("article", { class: "card", "data-type": typeId },
      h("div", { class: "card-head" }, h("h3", { text: group.type.name })),
      h("div", { class: "levels" }, group.levels.map(levelRow)));
    const body = h("div", { class: "more-body" });
    const details = h("details", { class: "more" }, h("summary", null, computed ? "모델별 예측 비교·발표기준" : "발표기준"), body);
    const key = `${state.regionId}/${typeId}`;
    const fill = () => renderTypeDetail(body, group, computed);
    details.addEventListener("toggle", () => {
      if (details.open) { state.open.add(key); fill(); } else state.open.delete(key);
    });
    if (state.open.has(key)) { details.open = true; fill(); }
    card.append(details);
    return card;
  }

  function renderHazards() {
    const r = region(), box = clear($("hazards")), latest = state.latest;
    $("horizon").textContent = latest.horizon_hours;
    const used = r.models || [];
    $("models-line").textContent = used.length
      ? `예측 자료 ${used.length}개 비교: ${used.map((id) => modelName(id, true)).join(" · ")}`
      : "이 지역의 예측 자료를 받지 못했습니다.";
    const groups = typeGroups(r);
    const notable = groups.filter(isNotable), quiet = groups.filter((g) => !isNotable(g));
    if (!notable.length) {
      box.append(h("div", { class: "panel" }, h("p", { class: "empty", text: `향후 ${latest.horizon_hours}시간 안에 기준에 가까운 예측이 없습니다.` })));
    }
    for (const group of notable) box.append(typeCard(group));
    if (quiet.length) {
      const names = quiet.map((g) => g.type.name).join(" · ");
      const holder = h("div", null);
      const toggle = h("button", { class: "chip", type: "button", "aria-expanded": String(state.showQuiet) });
      const paint = () => {
        clear(holder);
        toggle.textContent = state.showQuiet ? "접기" : "자세히 보기";
        toggle.setAttribute("aria-expanded", String(state.showQuiet));
        if (state.showQuiet) for (const group of quiet) holder.append(typeCard(group));
      };
      toggle.addEventListener("click", () => { state.showQuiet = !state.showQuiet; paint(); });
      box.append(h("div", { class: "panel quiet" }, h("p", null, h("b", null, "가능성 낮음: "), names, " ", toggle)), holder);
      paint();
    }
  }

  // ------------------------------------------------------------ 모델 비교(펼침)

  function renderTypeDetail(body, group, computed) {
    clear(body);
    const list = h("ul", { class: "criteria-text" });
    for (const { hazard } of group.levels) {
      list.append(h("li", null, h("b", null, `${hazard.name}: `), hazard.official_text, hazard.note ? h("span", { class: "note" }, ` ※ ${hazard.note}`) : null));
    }
    body.append(h("h4", { text: "발표기준" }), list);
    if (!computed) return;
    const holder = h("div", null, h("p", { class: "note", text: "모델별 자료를 불러오는 중…" }));
    body.append(holder);
    const regionId = state.regionId;
    loadDetail(regionId).then((detail) => {
      if (regionId !== state.regionId || !holder.isConnected) return;
      clear(holder);
      holder.append(compareTable(detail, group), chartBlock(detail, group));
    }).catch(() => {
      clear(holder).append(h("p", { class: "note", text: "모델별 자료를 불러오지 못했습니다. 잠시 뒤 다시 열어 보세요." }));
    });
  }

  /** 종류(예: 호우)의 모든 단계가 쓰는 기준 열을 모은다. 같은 지표·같은 연속 일수면 한 열. */
  function criteriaColumns(detail, group) {
    const columns = new Map();
    for (const { hazard } of group.levels) {
      const info = detail.hazards[hazard.id];
      if (!info) continue;
      for (const row of info.criteria) {
        const key = `${row.indicator}/${row.days}`;
        if (!columns.has(key)) columns.set(key, { key, indicator: row.indicator, days: row.days, meta: detail.indicators[row.indicator], perLevel: [] });
        columns.get(key).perLevel.push({ hazard, row });
      }
    }
    return [...columns.values()];
  }
  const opWord = { ">=": "이상", ">": "초과", "<=": "이하", "<": "미만" };

  function compareTable(detail, group) {
    const columns = criteriaColumns(detail, group);
    const head = h("tr", null, h("th", { scope: "col", text: "예측 자료" }),
      columns.map((c) => h("th", { scope: "col" }, c.meta.label, c.days > 1 ? h("small", null, `${c.days}일 연속 유지 수준`) : null, h("small", null, `단위 ${c.meta.unit}`))));
    const tbody = h("tbody");
    for (const id of region().models || []) {
      const row = h("tr", null, h("th", { scope: "row" }, h("span", { class: "key", style: `--key:${modelColor(id)}` }), modelName(id, true)));
      for (const column of columns) {
        const first = column.perLevel.map((x) => x.row.models[id]).find(Boolean);
        if (!first) { row.append(h("td", null, "–", h("small", null, "자료 없음"))); continue; }
        const over = column.perLevel.filter((x) => x.row.models[id] && x.row.models[id].x).sort((a, b) => levelRank(b.hazard.level) - levelRank(a.hazard.level))[0];
        row.append(h("td", { class: over ? "over" : null },
          over ? h("span", { class: "flag", title: `${levelName(over.hazard.level)} 기준 ${opWord[over.row.op]}`, text: levelName(over.hazard.level) }) : null,
          fmtNum(first.v, column.meta.decimals), h("small", null, fmtDayHour(first.t))));
      }
      tbody.append(row);
    }
    for (const { hazard } of group.levels) {
      const row = h("tr", { class: "limit" }, h("th", { scope: "row", text: `${levelName(hazard.level)} 기준` }));
      for (const column of columns) {
        const mine = column.perLevel.filter((x) => x.hazard.id === hazard.id);
        row.append(h("td", null, mine.length ? mine.map((x) => `${fmtNum(x.row.value, 1)} ${opWord[x.row.op]}${x.row.joint ? " (동시 충족)" : ""}`).join(", ") : "–"));
      }
      tbody.append(row);
    }
    return h("div", null,
      h("h4", { text: `자료별 최대 예측값 (향후 ${state.latest.horizon_hours}시간)` }),
      h("div", { class: "table-scroll" }, h("table", null, h("thead", null, head), tbody)),
      h("p", { class: "note" }, h("span", { class: "flag", text: "주의보" }), " ", h("span", { class: "flag", text: "경보" }), " 표시는 그 단계의 기준을 넘긴 값입니다. 아래 시각은 그 값이 나타나는 때입니다."));
  }

  function chartBlock(detail, group) {
    const columns = criteriaColumns(detail, group);
    const options = [];
    for (const column of columns) {
      const key = column.meta.chart;
      if (!key || !detail.series[key] || options.some((o) => o.key === key)) continue;
      const [kind, name] = key.split(":");
      options.push({ key, label: kind === "i" ? column.meta.label : VARIABLE_LABEL[name] || name, unit: column.meta.unit, decimals: column.meta.decimals });
    }
    const wrap = h("div", null);
    if (!options.length) return wrap;
    const pickKey = `${state.regionId}/${group.type.id}`;
    let current = options.find((o) => o.key === state.chartPick[pickKey]) || options[0];
    const area = h("div", null);
    const chips = h("div", { class: "chips", role: "group", "aria-label": "그래프로 볼 지표" });
    const paint = () => {
      clear(chips);
      if (options.length > 1) {
        for (const option of options) {
          chips.append(h("button", { class: "chip", type: "button", "aria-pressed": String(option === current), onclick: () => { current = option; state.chartPick[pickKey] = option.key; paint(); } }, option.label));
        }
      }
      // 이 계열에 직접 그을 수 있는 기준선: 시간 단위 지표는 그대로, 일 단위 최고·최저는 원 변수 그래프에 표시(전날 대비 변화는 제외)
      const limits = [];
      for (const column of columns) {
        if (column.meta.chart !== current.key || column.meta.kind === "period_change") continue;
        for (const { hazard, row } of column.perLevel) {
          if (!limits.some((l) => l.value === row.value)) limits.push({ value: row.value, op: row.op, label: `${levelName(hazard.level)} ${fmtNum(row.value, 1)}` });
        }
      }
      clear(area).append(lineChart({ detail, option: current, limits }));
    };
    wrap.append(h("h4", { text: "시간별 변화" }), chips, area);
    paint();
    return wrap;
  }

  // ------------------------------------------------------------ 선 그래프

  function niceTicks(min, max, count) {
    const span = max - min || 1;
    const raw = span / count, power = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((m) => m >= raw) || raw;
    const start = Math.floor(min / step) * step, ticks = [];
    for (let v = start; v <= max + step * 0.001; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
    if (ticks[ticks.length - 1] < max) ticks.push(Math.round((ticks[ticks.length - 1] + step) * 1e6) / 1e6);
    return ticks;
  }

  function lineChart({ detail, option, limits }) {
    const times = detail.times, seriesMap = detail.series[option.key];
    const ids = (region().models || []).filter((id) => seriesMap[id]);
    const values = ids.flatMap((id) => seriesMap[id]).filter((v) => v != null);
    const container = h("div", { class: "chart" });
    const figure = h("figure", { style: "margin:0" }, container);
    if (!values.length) { container.append(h("p", { class: "note", text: "표시할 자료가 없습니다." })); return figure; }

    // 세로 범위: 자료 + 가장 가까운 기준선(다른 기준선은 자료가 그 근처까지 갈 때만)
    let lo = Math.min(...values), hi = Math.max(...values);
    const zeroBased = option.key.startsWith("i:") || lo >= 0 && option.unit !== "℃";
    const upper = limits.filter((l) => l.op.startsWith(">")).sort((a, b) => a.value - b.value);
    const lower = limits.filter((l) => l.op.startsWith("<")).sort((a, b) => b.value - a.value);
    const shown = [];
    upper.forEach((l, i) => { if (i === 0 || hi >= l.value * 0.7) { shown.push(l); hi = Math.max(hi, l.value); } });
    lower.forEach((l, i) => { if (i === 0 || lo <= l.value + 3) { shown.push(l); lo = Math.min(lo, l.value); } });
    if (zeroBased) lo = Math.min(0, lo);
    const pad = (hi - lo || 1) * 0.08;
    const ticks = niceTicks(zeroBased ? lo : lo - pad, hi + pad, 4);
    const y0 = ticks[0], y1 = ticks[ticks.length - 1];

    const W = Math.max(280, Math.round(container.clientWidth || document.querySelector(".more-body")?.clientWidth || 328));
    const H = 232, M = { top: 16, right: 14, bottom: 40, left: 40 };
    const plotW = W - M.left - M.right, plotH = H - M.top - M.bottom;
    const x = (i) => M.left + (plotW * i) / (times.length - 1);
    const y = (v) => M.top + plotH * (1 - (v - y0) / (y1 - y0));

    const peakId = ids.reduce((best, id) => (Math.max(...seriesMap[id].filter((v) => v != null)) > Math.max(...seriesMap[best].filter((v) => v != null)) ? id : best), ids[0]);
    const peakValue = Math.max(...seriesMap[peakId].filter((v) => v != null));
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", tabindex: "0",
      "aria-label": `${option.label} 예측 자료별 시간 변화. 가장 높은 값은 ${modelName(peakId, true)} ${fmtNum(peakValue, option.decimals)}${option.unit}. 좌우 화살표로 시각을 옮겨 값을 읽을 수 있습니다.` });

    const grid = s("g", { class: "grid" });
    for (const tick of ticks) {
      grid.append(s("line", { x1: M.left, x2: W - M.right, y1: y(tick), y2: y(tick) }));
      svg.append(s("text", { class: "tick", x: M.left - 6, y: y(tick) + 4, "text-anchor": "end" }, fmtNum(tick, 1)));
    }
    svg.prepend(grid);
    svg.append(s("text", { class: "tick", x: M.left - 6, y: 9, "text-anchor": "end" }, option.unit));
    svg.append(s("line", { class: "axis-line", x1: M.left, x2: W - M.right, y1: M.top + plotH, y2: M.top + plotH }));

    const stepHours = plotW < 420 ? 12 : 6;
    times.forEach((iso, i) => {
      const p = kst(iso);
      if (p.hour % stepHours === 0) {
        svg.append(s("line", { class: "axis-line", x1: x(i), x2: x(i), y1: M.top + plotH, y2: M.top + plotH + 4 }));
        svg.append(s("text", { class: "tick", x: x(i), y: M.top + plotH + 16, "text-anchor": "middle" }, `${two(p.hour)}시`));
      }
      if (p.hour === 0 || i === 0) {
        const anchor = i === 0 ? "start" : "middle";
        if (i === 0 && p.hour > 24 - stepHours) return;
        svg.append(s("text", { class: "day", x: x(i), y: M.top + plotH + 31, "text-anchor": anchor }, `${p.day}일(${p.weekday})`));
      }
    });

    for (const limit of shown) {
      svg.append(s("line", { class: "limit-line", x1: M.left, x2: W - M.right, y1: y(limit.value), y2: y(limit.value) }));
      svg.append(s("text", { class: "limit-label", x: W - M.right - 2, y: y(limit.value) - 5, "text-anchor": "end" }, limit.label));
    }

    for (const id of ids) {
      let d = "", pen = false;
      seriesMap[id].forEach((v, i) => {
        if (v == null) { pen = false; return; }
        d += `${pen ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
        pen = true;
      });
      svg.append(s("path", { class: "series", d, stroke: modelColor(id) }));
    }

    // 가리킨 시각의 세로선·점·값 상자
    const cross = s("g", { visibility: "hidden" }, s("line", { class: "cross", y1: M.top, y2: M.top + plotH }));
    const dots = ids.map((id) => { const dot = s("circle", { class: "dot", r: 4.5, fill: modelColor(id) }); cross.append(dot); return dot; });
    svg.append(cross, s("rect", { x: M.left, y: M.top, width: plotW, height: plotH, fill: "transparent" }));
    const tip = h("div", { class: "tip", hidden: true });
    let index = -1;
    const show = (i) => {
      index = Math.max(0, Math.min(times.length - 1, i));
      cross.setAttribute("visibility", "visible");
      cross.firstChild.setAttribute("x1", x(index));
      cross.firstChild.setAttribute("x2", x(index));
      clear(tip).append(h("p", { class: "tip-time", text: fmtDayHour(times[index]) }));
      ids.forEach((id, k) => {
        const v = seriesMap[id][index];
        dots[k].setAttribute("visibility", v == null ? "hidden" : "inherit");
        if (v != null) { dots[k].setAttribute("cx", x(index)); dots[k].setAttribute("cy", y(v)); }
        tip.append(h("div", { class: "tip-row" }, h("span", null, h("span", { class: "key", style: `--key:${modelColor(id)}` }), modelName(id, true)), h("b", null, v == null ? "–" : `${fmtNum(v, option.decimals)}${option.unit}`)));
      });
      tip.hidden = false;
      const scale = (svg.getBoundingClientRect().width || W) / W, px = x(index) * scale, width = tip.offsetWidth || 160;
      tip.style.left = `${px + 12 + width > W * scale ? Math.max(0, px - width - 12) : px + 12}px`;
      tip.style.top = `${M.top}px`;
    };
    const hide = () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; index = -1; };
    const fromPointer = (event) => {
      const box = svg.getBoundingClientRect();
      const ratio = ((event.clientX - box.left) / box.width * W - M.left) / plotW;
      show(Math.round(ratio * (times.length - 1)));
    };
    svg.addEventListener("pointermove", fromPointer);
    svg.addEventListener("pointerdown", fromPointer);
    svg.addEventListener("pointerleave", (event) => { if (event.pointerType === "mouse") hide(); });
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", (event) => {
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") { event.preventDefault(); show((index < 0 ? 0 : index) + (event.key === "ArrowRight" ? 1 : -1)); }
      else if (event.key === "Escape") hide();
    });
    container.append(svg, tip);

    const legend = h("ul", { class: "legend" }, ids.map((id) => h("li", null, h("span", { class: "key", style: `--key:${modelColor(id)}` }), modelName(id, true))));
    if (shown.length) legend.append(h("li", null, h("span", { class: "key limit-key" }), "특보 기준"));
    const missing = (region().models || []).filter((id) => !seriesMap[id]).map((id) => modelName(id, true));
    figure.append(legend);
    if (missing.length) figure.append(h("p", { class: "note", text: `${missing.join(", ")}: 이 지표의 자료를 제공하지 않습니다.` }));

    const rows = [];
    times.forEach((iso, i) => {
      if (i % 3) return;
      rows.push(h("tr", null, h("th", { scope: "row", text: fmtDayHour(iso) }), ids.map((id) => h("td", null, seriesMap[id][i] == null ? "–" : fmtNum(seriesMap[id][i], option.decimals)))));
    });
    figure.append(h("details", { class: "data-table" }, h("summary", { text: `표로 보기 (3시간 간격, ${option.unit})` }),
      h("div", { class: "table-scroll" }, h("table", null, h("thead", null, h("tr", null, h("th", { scope: "col", text: "시각" }), ids.map((id) => h("th", { scope: "col", text: modelName(id, true) })))), h("tbody", null, rows)))));
    return figure;
  }

  // ------------------------------------------------------------ 전체 지역

  function renderOverview() {
    const latest = state.latest, grid = clear($("region-grid"));
    for (const r of latest.regions) {
      const top = r.top;
      const grade = top ? top.grade : "unknown";
      const hazard = top ? latest.hazards.find((hz) => hz.id === top.hazard) : null;
      let sub = "예측 자료 없음";
      if (top) sub = GRADE_RANK[grade] >= GRADE_RANK.caution && hazard ? `${hazard.name}${top.p != null && grade !== "active" ? ` ${top.p}%` : ""}` : "기준에 가까운 예측 없음";
      grid.append(h("button", { class: "region-btn", type: "button", "data-grade": grade, "aria-current": String(r.id === state.regionId), onclick: () => { selectRegion(r.id); $("region").focus({ preventScroll: true }); window.scrollTo({ top: 0, behavior: "smooth" }); } },
        h("span", { class: "rname", text: r.name }), badge(grade), h("span", { class: "rsub", text: sub })));
    }
  }

  // ------------------------------------------------------------ 적중률

  // ------------------------------------------------------------ 철도 구간 전망

  /** 계산 대상 지역 가운데 ‘주의’ 이상인 곳을 지나는 노선. 지역의 대표 특보(가장 높은 등급)를 그대로 쓴다 — 새로 계산하지 않는다. */
  function renderRailOutlook() {
    const box = clear($("rail-outlook")), latest = state.latest, geo = state.geo;
    $("horizon2").textContent = latest.horizon_hours;
    if (!geo) { box.append(h("p", { class: "empty", text: "노선 자료를 불러오는 중…" })); return; }
    if (geo.failed) { box.append(h("p", { class: "empty", text: "노선 자료를 불러오지 못했습니다." })); return; }
    const regionByZone = new Map();
    for (const r of latest.regions) for (const code of r.zones || []) if (!regionByZone.has(code)) regionByZone.set(code, r);
    const hazardName = Object.fromEntries(latest.hazards.map((hz) => [hz.id, hz.name]));
    const rows = [];
    let covered = 0;
    for (const line of geo.lines) {
      const items = [];  // 노선이 지나는 순서대로, 지역마다 한 번
      for (const [zone, keys] of line.parts) {
        const r = regionByZone.get(zone);
        if (!r) continue;
        let item = items.find((x) => x.region === r);
        if (!item) { item = { region: r, stations: [] }; items.push(item); }
        for (const key of keys) { const name = geo.stationName.get(key); if (name && !item.stations.includes(name)) item.stations.push(name); }
      }
      if (items.length) covered += 1;
      const hot = items.filter((x) => x.region.top && GRADE_RANK[x.region.top.grade] >= GRADE_RANK.caution);
      if (hot.length) rows.push({ line, hot, top: Math.max(...hot.map((x) => GRADE_RANK[x.region.top.grade])), p: Math.max(...hot.map((x) => x.region.top.p || 0)) });
    }
    rows.sort((a, b) => b.top - a.top || b.p - a.p || a.line.name.localeCompare(b.line.name, "ko"));
    if (!rows.length) {
      box.append(h("p", { class: "empty", text: `향후 ${latest.horizon_hours}시간 안에 ‘주의’ 이상으로 계산된 철도 구간이 없습니다.` }),
        h("p", { class: "note", text: `계산 대상 지역을 지나는 노선 ${covered}개를 확인했습니다.` }));
      return;
    }
    const list = h("ul", { class: "plain outlook" });
    for (const { line, hot } of rows) {
      list.append(h("li", null, h("h3", null, line.name, line.fast ? h("small", null, " 고속선") : null),
        h("ul", { class: "plain" }, hot.map(({ region: r, stations }) => {
          const sum = r.hazards[r.top.hazard] || {};
          const upcoming = sum.g === "active" && isFuture(sum.official_from);
          const when = sum.g === "active" ? (sum.official_from ? `${fmtDayHour(sum.official_from)} ${upcoming ? "발효 예정" : "발효"}` : "") : sum.from ? `${fmtRange(sum.from, sum.to)} 기준 도달 예상` : "";
          return h("li", { class: "outlook-item" },
            badge(r.top.grade, upcoming ? "발효 예정" : GRADE_LABEL[r.top.grade]),
            h("div", null,
              h("p", { class: "outlook-main" }, h("button", { class: "linklike", type: "button", onclick: () => { selectRegion(r.id); window.scrollTo({ top: 0, behavior: "smooth" }); } }, r.name),
                ` · ${hazardName[r.top.hazard] || r.top.hazard}`, r.top.p != null && r.top.grade !== "active" ? ` ${r.top.p}%` : ""),
              h("p", { class: "outlook-sub", text: [when, stations.length ? `구간의 역: ${stations.join(", ")}` : "구간에 역 없음"].filter(Boolean).join(" · ") })));
        }))));
    }
    box.append(list, h("p", { class: "note" }, "지역별 가능성을 노선에 옮겨 적은 것입니다(시·군청 위치 기준). 지금 발효 중인 전국 특보와 노선은 ", h("a", { href: "rail.html" }, "지금 지도"), "에서, 과거 특보 때의 열차 지연은 ", h("a", { href: "analysis.html" }, "분석"), "에서 봅니다."));
  }

  // ------------------------------------------------------------ 계산 방식·출처

  function renderAbout() {
    const latest = state.latest, box = clear($("about")), sc = latest.scoring, src = latest.sources || {};
    const stateWord = (ok, configured) => (ok ? "정상" : configured === false ? "인증키 미설정" : "실패");
    const sourceLine = (label, info, extra) => h("li", null, `${label}: `, h("span", { class: "state", text: stateWord(info && info.ok, info && info.configured) }), extra || "", info && !info.ok && info.error && info.configured !== false ? ` — ${info.error}` : "");

    box.append(h("details", null, h("summary", { text: "가능성(%)은 이렇게 계산합니다" }), h("div", { class: "inner" },
      h("p", null, `예측 자료마다 향후 ${latest.horizon_hours}시간의 시간별 값으로 3·12·24시간 누적, 일 최고·최저 등을 구해 특보 기준과 비교합니다.`),
      h("ul", null,
        h("li", null, h("b", null, "도달률"), " = 자료별 최대 예측값 ÷ 기준값 (기온처럼 0이 의미 없는 지표는 기준 아래 일정 폭을 0%로 잡습니다)"),
        h("li", null, h("b", null, "초과 비율"), " = 기준을 넘긴 자료 수 ÷ 전체 자료 수")),
      h("p", { class: "formula" }, `가능성 = ${Math.round(sc.weight_exceed_ratio * 100)}% × 초과 비율 + ${Math.round(sc.weight_max_reach * 100)}% × 점수(최대 도달률) + ${Math.round(sc.weight_mean_reach * 100)}% × 점수(평균 도달률)`, h("br"), `점수: 도달률 ${pct(sc.reach_floor)} 이하는 0점, 100% 이상은 만점, 그 사이는 비례`),
      h("p", null, `등급: ${latest.grades.caution_min}% 미만 ‘낮음’ · ${latest.grades.caution_min}% 이상 ‘주의’ · ${latest.grades.high_min}% 이상 ‘높음’. 기상청 예비특보가 있으면 ‘높음’ 이상으로, 특보가 발표되어 있으면 ‘발효 중’으로 표시합니다.`),
      h("p", null, "예상 시간대는 자료별로 기준을 처음 넘는 시각의 범위입니다(누적 기준은 누적값이 기준에 닿는 시각)."))));

    box.append(h("details", null, h("summary", { text: "자료 출처와 이번 수집 상태" }), h("div", { class: "inner" },
      h("ul", null,
        sourceLine("기상청 기상특보·예비특보", src.kma_warnings, src.kma_warnings && src.kma_warnings.provider ? ` (${src.kma_warnings.provider})` : ""),
        sourceLine("기상청 단기예보", src.kma_forecast, src.kma_forecast && src.kma_forecast.base ? ` (${src.kma_forecast.provider}, ${src.kma_forecast.base.replace(/^(\d{4})(\d{2})(\d{2}) (\d{2})(\d{2})$/, "$2월 $3일 $4시 발표")})` : ""),
        sourceLine("Open-Meteo 수치모델", src.open_meteo, src.open_meteo ? ` (${src.open_meteo.models_ok}/${src.open_meteo.models_total}개 모델)` : "")),
      h("p", null, "비교하는 예측 자료:"),
      h("ul", null, latest.models.map((m) => h("li", null, h("span", { class: "key", style: `--key:${modelColor(m.id)}` }), `${m.name}: `, h("span", { class: "state", text: stateWord(m.ok, m.configured) })))),
      h("p", null, "기상자료 제공: 기상청(공공데이터포털, 기상청 API허브) · ", h("a", { href: "https://open-meteo.com/", rel: "noopener" }, "Weather data by Open-Meteo.com"), " (", h("a", { href: "https://creativecommons.org/licenses/by/4.0/", rel: "noopener" }, "CC BY 4.0"), "). 원자료를 가공해 표시합니다."))));

    box.append(h("details", null, h("summary", { text: "특보 기준과 한계" }), h("div", { class: "inner" },
      h("p", null, `기준 출처: ${latest.thresholds.source.replace(/https?:\/\/\S+/g, "").replace(/\s+·/g, " ·").trim()} (기준 판 ${latest.thresholds.version}, ${latest.thresholds.checked_at} 확인) · `, h("a", { href: STANDARD_URL, rel: "noopener" }, "날씨누리 기상특보 발표기준")),
      h("ul", null,
        h("li", null, "실제 특보는 예보관이 관측·영향을 종합해 발표합니다. 기준에 닿아도 발표되지 않거나, 기준 미만이어도 유지될 수 있습니다."),
        h("li", null, "수치모델은 좁은 지역의 집중호우·돌풍을 낮게 예측하는 경향이 있습니다. ‘낮음’이 안전을 뜻하지는 않습니다."),
        h("li", null, "지점 예측값(시청·군청 위치)이라 같은 시·군 안에서도 산지·해안·섬은 다를 수 있습니다."),
        h("li", null, "태풍·건조·풍랑·황사·폭풍해일은 계산하지 않고 기상청 발표만 표시합니다."),
        h("li", null, "자료는 3시간마다 갱신됩니다. 급변하는 상황은 기상청 발표를 직접 확인하세요.")))));
  }

  // ------------------------------------------------------------ 전체 그리기·지역 선택

  function renderRegionSelect() {
    const select = clear($("region"));
    for (const r of state.latest.regions) select.append(h("option", { value: r.id, text: r.name }));
    select.value = state.regionId;
  }

  function renderRegion() {
    renderOfficial();
    renderNow();
    renderHazards();
    for (const button of document.querySelectorAll(".region-btn")) button.setAttribute("aria-current", "false");
    const index = state.latest.regions.findIndex((r) => r.id === state.regionId);
    const current = document.querySelectorAll(".region-btn")[index];
    if (current) current.setAttribute("aria-current", "true");
    document.title = `${region().name} 기상특보 가능성`;
  }

  function selectRegion(id, remember = true) {
    if (!state.latest.regions.some((r) => r.id === id)) id = state.latest.regions[0].id;
    state.regionId = id;
    state.showQuiet = false;
    $("region").value = id;
    if (remember) {
      try { localStorage.setItem("teukbo.region", id); } catch (_) { /* 저장소를 못 써도 화면은 동작한다 */ }
      if (location.hash.slice(1) !== id) history.replaceState(null, "", `#${id}`);
    }
    renderRegion();
  }

  function initialRegion() {
    const fromHash = decodeURIComponent(location.hash.slice(1));
    if (state.latest.regions.some((r) => r.id === fromHash)) return fromHash;
    try {
      const saved = localStorage.getItem("teukbo.region");
      if (state.latest.regions.some((r) => r.id === saved)) return saved;
    } catch (_) { /* 무시 */ }
    return state.latest.regions[0].id;
  }

  function renderAll() {
    if (!state.regionId || !state.latest.regions.some((r) => r.id === state.regionId)) state.regionId = initialRegion();
    renderUpdated();
    renderAlerts();
    renderRegionSelect();
    renderOverview();
    renderRegion();
    renderRailOutlook();
    renderAbout();
  }

  // ------------------------------------------------------------ 시작

  $("region").addEventListener("change", (event) => selectRegion(event.target.value));
  window.addEventListener("hashchange", () => {
    const id = decodeURIComponent(location.hash.slice(1));
    if (state.latest && id !== state.regionId && state.latest.regions.some((r) => r.id === id)) selectRegion(id, false);
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { loadLatest(); loadLive(); } });
  setInterval(() => { if (!document.hidden) loadLatest(); }, REFRESH_MS);
  // 실시간 자료는 더 자주 읽는다. 읽지 못해도 1분마다 '몇 분 전 확인'과 지연 여부는 다시 판단한다.
  let liveTimer = 0;
  setInterval(() => {
    if (!state.base) return;
    const every = Math.max(30, (liveConfig() || {}).poll_seconds || 120);
    liveTimer += 60;
    if (!document.hidden && liveConfig() && liveTimer >= every) { liveTimer = 0; loadLive(); } else refreshLive();
  }, 60 * 1000);
  // 노선이 지나는 구역·역(철도 구간 전망용)은 첫 화면이 뜬 뒤에 받는다
  Promise.all([getJson("geo/lines.json"), getJson("geo/stations.json")]).then(([lines, stations]) => {
    state.geo = { lines, stationName: new Map(stations.map((st) => [st.code || `name:${st.name}`, st.name.replace(/^S(?=[가-힣])/, "")])) };
    if (state.latest) renderRailOutlook();
  }).catch(() => { state.geo = { lines: [], stationName: new Map(), failed: true }; if (state.latest) renderRailOutlook(); });
  let resizeTimer = 0, lastWidth = window.innerWidth;
  window.addEventListener("resize", () => {
    if (window.innerWidth === lastWidth) return;
    lastWidth = window.innerWidth;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.latest) renderHazards(); }, 200);
  });

  loadLatest();

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => { /* 오프라인 지원 없이 계속 */ }));
  }
})();
