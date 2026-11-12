/* 분석 — 과거 자료 탐색. 조건(기간·특보·노선·열차 종류)을 고르면 그 조건의 열차 지연·운행률을 숫자와 그래프로 보여 준다.
 *
 * 자료
 *  data/rail-cube.json            날짜 × 노선 × 열차 종류 × 지나간 특보 단위로 묶은 집계 (teukbo.rail.build_cube)
 *  data/rail-days/<날짜>.json     그날의 열차 목록 — 날짜를 골랐을 때만 읽는다
 *  data/accuracy.json             특보 가능성 예측의 적중률
 * 화면의 모든 숫자는 위 조건으로 같은 묶음에서 다시 센 것이라 서로 맞아떨어진다.
 * 판정(더 늦었음·차이 없음·자료 부족)의 규칙은 서버(teukbo.rail.adjusted_difference)와 같고, 테스트가 두 결과를 대조한다.
 */
(() => {
  "use strict";
  const { h, s, $, clear, fmtClock, fmtDate, fmtAgo, num, stationLabel, getJson } = window.TeukboUI;
  const VERDICT = { later: "더 늦었음", earlier: "덜 늦었음", no_difference: "뚜렷한 차이 없음", insufficient: "자료 부족" };
  const TYPE_ORDER = ["호우", "태풍", "강풍", "대설", "한파", "폭염", "열대야", "건조", "풍랑", "황사", "폭풍해일"];
  const REFRESH_MS = 30 * 60 * 1000;
  const state = { cube: null, accuracy: null, latest: null, filter: { period: "all", from: null, to: null, type: "all", line: "all", cls: "all" }, day: null, days: {}, query: "", warnedOnly: false, showAll: false };

  /** 여러 자식을 한 번에 붙인다(배열은 펴고, 없는 것은 건너뛴다). DOM 의 append 는 배열·null 을 글자로 바꿔 버린다. */
  const put = (node, ...kids) => { node.append(...kids.flat(Infinity).filter((kid) => kid != null && kid !== false)); return node; };
  const count = (n) => Number(n || 0).toLocaleString("ko-KR");
  const pct = (part, total) => (total ? `${Math.round((part / total) * 100)}%` : "–");
  /** 분 단위 값은 늘 소수 첫째 자리까지 — 세로로 늘어놓았을 때 자릿수가 맞아야 견주기 쉽다. */
  const mins = (value) => (value == null ? "–" : Number(value).toFixed(1));
  const smins = (value) => (value == null ? "–" : `${value >= 0.05 ? "+" : value <= -0.05 ? "−" : ""}${Math.abs(value).toFixed(1)}`);
  /** 운행률(%). 반올림하되, 운행 기록이 없는 열차가 한 편이라도 있으면 100.0% 로 보이지 않게 한다. */
  /** 평소 같은 요일 대비 운행 열차 수(%) — 운휴로 계획에서 빠진 열차까지 드러난다. 평소 값을 낼 수 없으면 "–" */
  const versus = (ran, usual) => (usual ? `${((ran / usual) * 100).toFixed(1)}%` : "–");
  const rate = (planned, noRecord) => (planned ? `${(noRecord > 0 ? Math.min(99.9, Math.round(((planned - noRecord) / planned) * 1000) / 10) : 100).toFixed(1)}%` : "–");

  // 다시 세기(조건에 맞춰 묶음 자료를 집계)는 analysis-core.js 가 맡는다 — 화면 없이도 돌려 볼 수 있게 따로 두었다.
  const { stats, typeOf, MIN_SAMPLE, MIN_EFFECT } = window.TeukboAnalysisCore;
  const compute = () => window.TeukboAnalysisCore.compute(state.cube, state.filter);

  // ------------------------------------------------------------ 조건 고르기

  function renderFilters() {
    const cube = state.cube, f = state.filter;
    const chip = (box, value, label, key) => box.append(h("button", { class: "chip", type: "button", "aria-pressed": String(f[key] === value), onclick: () => { f[key] = value; if (key === "period") syncCustom(); update(); } }, label));
    const period = clear($("f-period"));
    for (const [value, label] of [["7", "최근 7일"], ["30", "최근 30일"], ["all", "전체 기간"], ["custom", "직접 고르기"]]) chip(period, value, label, "period");
    const types = [...new Set(cube.warnings.map(typeOf))].sort((a, b) => (TYPE_ORDER.indexOf(a) + 99) % 99 - (TYPE_ORDER.indexOf(b) + 99) % 99);
    if (f.type !== "all" && !types.includes(f.type)) f.type = "all";
    const type = clear($("f-type"));
    chip(type, "all", "모든 특보", "type");
    for (const name of types) chip(type, name, name, "type");
    if (!types.length) type.append(h("span", { class: "hint", text: "이 기간의 운행 기록에는 특보 구간을 지난 열차가 없습니다." }));
    const fill = (select, items, key, all) => {
      put(clear(select), h("option", { value: "all", text: all }), items.map((name) => h("option", { value: name, text: name })));
      if (f[key] !== "all" && !items.includes(f[key])) f[key] = "all";
      select.value = f[key];
    };
    fill($("f-line"), [...cube.lines].sort((a, b) => a.localeCompare(b, "ko")), "line", "모든 노선");
    fill($("f-class"), [...cube.classes].sort((a, b) => a.localeCompare(b, "ko")), "cls", "모든 열차");
    syncCustom();
  }

  function syncCustom() {
    const days = state.cube.days, first = days[0].date, last = days[days.length - 1].date, f = state.filter;
    $("f-custom").hidden = f.period !== "custom";
    for (const [id, key, fallback] of [["f-from", "from", first], ["f-to", "to", last]]) {
      const input = $(id);
      input.min = first; input.max = last;
      if (!f[key]) f[key] = fallback;
      input.value = f[key];
    }
  }

  function scopeText(r) {
    const f = state.filter, days = Math.round((new Date(r.to) - new Date(r.from)) / 86400000) + 1;
    return [`${fmtDate(r.from)} ~ ${fmtDate(r.to)} (${days}일)`, f.type === "all" ? "모든 특보" : `${f.type} 특보`, f.line === "all" ? "모든 노선" : f.line, f.cls === "all" ? "모든 열차" : f.cls].join(" · ");
  }

  // ------------------------------------------------------------ 결과 요약

  function renderKpis(r) {
    const box = clear($("kpis")), all = stats(r.all), warn = stats(r.warn), rest = stats(r.clear), f = state.filter;
    const tile = (label, who, value, unit, subs) => put(h("div", { class: "tile" }), h("p", { class: "label" }, label, h("small", null, who)),
      h("p", { class: value == null ? "value pending" : "value" }, value == null ? "–" : value, value != null && unit ? h("small", null, unit) : null), subs.filter(Boolean).map((text) => h("p", { class: "sub", text })));
    const kind = f.type === "all" ? "특보" : `${f.type}특보`, what = `${kind} 구간을 지난 열차`;
    const hit = r.hitDays, calm = r.calmDays, other = all.n - warn.n - rest.n;
    put(box,
      tile("조건에 맞는 열차", "종착 지연을 계산한 열차", count(all.n), "편", [`${kind} 구간 통과 ${count(warn.n)}편`, `특보 없던 열차 ${count(rest.n)}편`, other > 0 ? `다른 특보만 지난 ${count(other)}편은 비교에서 제외` : null]),
      tile("운행률", "계획 대비 · 전체 열차 기준", r.planned ? rate(r.planned, r.noRecord).replace("%", "") : null, "%",
        r.planned ? [`계획 ${count(r.planned)}편 중 ${count(r.planned - r.noRecord)}편 운행`, hit.days ? `${kind} 걸린 날 ${rate(hit.planned, hit.noRecord)}` : `${kind} 걸린 날 없음`] : ["계획 자료 없음"]),
      tile("평균 종착 지연", what, warn.n ? mins(warn.mean) : null, "분", warn.n ? [`특보 없던 열차 ${rest.n ? `${mins(rest.mean)}분` : "–"}`, `가장 늦은 열차 ${warn.max}분`] : ["해당 열차 없음"]),
      tile("10분 이상 늦은 비율", what, warn.n ? String(Math.round((warn.late10 / warn.n) * 100)) : null, "%", warn.n ? [`${count(warn.n)}편 중 ${count(warn.late10)}편`, `특보 없던 열차 ${pct(rest.late10, rest.n)}`] : ["해당 열차 없음"]));

    const adj = r.adjusted, panel = clear($("verdict"));
    const interval = adj.diff == null ? "" : `95% 구간 ${smins(adj.low)}~${smins(adj.high)}분`;
    const sentence = {
      later: `${what} ${count(adj.n)}편은 같은 노선의 특보 없던 열차보다 평균 ${mins(adj.diff)}분 더 늦게 도착했습니다 (${interval}).`,
      earlier: `${what} ${count(adj.n)}편은 같은 노선의 특보 없던 열차보다 오히려 평균 ${mins(Math.abs(adj.diff))}분 덜 늦었습니다 (${interval}).`,
      no_difference: `${what} ${count(adj.n)}편과 같은 노선의 특보 없던 열차 사이에 뚜렷한 지연 차이가 없었습니다 (차이 ${smins(adj.diff)}분, ${interval}).`,
      insufficient: adj.n ? `${what}가 ${count(adj.n)}편뿐이라 지연 차이를 판단하기에는 자료가 부족합니다${adj.diff == null ? "" : ` (지금까지의 차이 ${smins(adj.diff)}분, ${interval})`}.` : `이 조건에는 ${what}가 없습니다. 기간을 넓히거나 다른 특보·노선을 골라 보세요.`,
    }[adj.verdict];
    panel.append(h("p", { class: "verdict-line" }, h("span", { class: `pill verdict-${adj.verdict}`, text: VERDICT[adj.verdict] }), " ", sentence),
      h("p", { class: "note", text: "같은 노선끼리 비교한 값입니다(노선마다 평소 지연이 달라서). ‘같은 때 있었다’는 사실이며, 특보가 지연의 원인이라는 뜻은 아닙니다." }));

    // 운행: 특보가 걸린 날과 그 밖의 날. 계획 대비 운행률과, 평소 같은 요일 대비 열차 수를 나란히 둔다.
    const op = clear($("operation"));
    // 칸마다 무엇의 값인지 작은 글로 다시 적는다 — 좁은 화면에서 표 머리와 값이 멀어지지 않게.
    const opCell = (what, basis, value, detail, low) => h("div", { class: "op-cell" }, h("span", { class: "op-k" }, h("em", null, what), h("i", null, basis)), h("b", null, value), h("small", null, detail), low ? h("small", { class: "tag", text: "평소보다 적음" }) : null);
    const opRow = (label, o) => h("li", { class: "op-row" }, h("div", { class: "op-label" }, h("b", null, label), h("small", null, `${count(o.days)}일`)),
      opCell("운행률", "계획 대비", rate(o.planned, o.noRecord), o.planned ? `${count(o.planned - o.noRecord)} / ${count(o.planned)}편` : "해당 날짜 없음"),
      opCell("운행 열차 수", "평소 같은 요일 대비", versus(o.ran, o.usual), o.usual ? `${count(o.ran)} / 평소 ${count(Math.round(o.usual))}편` : "평소 값 없음", Boolean(o.usual) && o.ran / o.usual < 0.97));
    put(op, h("h3", { class: "panel-h", text: "운행 — 특보가 걸린 날과 그 밖의 날" }), h("ul", { class: "op-list" }, opRow(`${kind} 걸린 날`, hit), opRow("그 밖의 날", calm)),
      h("p", { class: "note", text: `‘${kind} 걸린 날’은 조건에 맞는 열차가 ${kind} 구간을 지난 날입니다. 운행은 그날 전체 열차 기준입니다(계획 자료에 노선이 없음). 운휴 열차가 계획 자료에서 빠지는 경우에도 ‘운행 열차 수’가 평소보다 줄어든 것으로 드러납니다. 평소 = 확정된 날들의 같은 요일 중앙값이며, 명절·임시열차가 있는 날은 다를 수 있습니다.` }));
  }

  // ------------------------------------------------------------ 막대 그래프 (공용)

  /** 세로 막대 그래프. groups: [{label(축 글), title(풍선·표 머리), values: [숫자…], mark(위에 붙일 표시), picked, onPick}], series: [{name, cls}]
   *  값은 풍선(가리키거나 초점)과 아래 '표로 보기'로도 읽을 수 있다. */
  function barChart({ groups, series, unit, label, tipRows, tableHead, tableRow, tickEvery }) {
    const wrap = h("figure", { class: "figure" }), chart = h("div", { class: "chart" });
    const W = Math.max(300, Math.min(720, ($("main").clientWidth || 640) - 34)), H = 220, M = { top: 26, right: 14, bottom: 30, left: 34 };
    const max = Math.max(1, ...groups.flatMap((g) => g.values.map((v) => v || 0)));
    const step = [1, 2, 5, 10, 20, 25, 50, 100, 200].find((x) => max / x <= 5) || 500, top = Math.ceil(max / step) * step;
    const plotW = W - M.left - M.right, plotH = H - M.top - M.bottom, band = plotW / groups.length;
    // 계열이 둘 이상이면 묶음 안은 2px, 묶음 사이는 넉넉히 띄워 어느 막대끼리 짝인지 바로 보이게 한다
    const inner = Math.max(1, Math.min(24 * series.length + 2 * (series.length - 1), series.length > 1 ? band * 0.68 : band - 2)), bar = (inner - 2 * (series.length - 1)) / series.length;
    const y = (v) => M.top + plotH * (1 - v / top);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": label });
    const grid = s("g", { class: "grid" });
    for (let v = 0; v <= top; v += step) { grid.append(s("line", { x1: M.left, x2: W - M.right, y1: y(v), y2: y(v) })); svg.append(s("text", { class: "tick", x: M.left - 6, y: y(v) + 4, "text-anchor": "end" }, String(v))); }
    svg.prepend(grid);
    svg.append(s("text", { class: "tick", x: M.left - 6, y: 11, "text-anchor": "end" }, unit));
    const tip = h("div", { class: "tip", hidden: true });
    const every = tickEvery || Math.max(1, Math.ceil(groups.length / Math.floor(plotW / 44)));
    groups.forEach((g, i) => {
      const cx = M.left + band * (i + 0.5), x0 = cx - inner / 2;
      const show = () => {
        put(clear(tip), h("p", { class: "tip-time", text: g.title }), tipRows(g).map(([name, value, cls]) => h("div", { class: "tip-row" }, h("span", null, cls ? h("i", { class: `tip-key ${cls}` }) : null, name), h("b", null, value))));
        tip.hidden = false;
        const scale = svg.getBoundingClientRect().width / W, width = tip.offsetWidth || 160;
        tip.style.left = `${Math.max(0, Math.min(cx * scale - width / 2, W * scale - width))}px`;
        tip.style.top = "0px";
      };
      const hide = () => { tip.hidden = true; };
      const node = s("g", { class: `bar-g${g.picked ? " picked" : ""}${g.onPick ? " pickable" : ""}`, tabindex: "0", role: g.onPick ? "button" : "img", "aria-label": `${g.title}: ${tipRows(g).map(([name, value]) => `${name} ${value}`).join(", ")}`,
        onpointerenter: show, onfocus: show, onpointerleave: hide, onblur: hide,
        onclick: g.onPick ? () => g.onPick() : null, onkeydown: g.onPick ? (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); g.onPick(); } } : null },
        s("rect", { x: cx - band / 2, y: M.top, width: band, height: plotH, fill: "transparent" }));
      g.values.forEach((value, k) => {
        if (!(value > 0)) return;
        const x = x0 + k * (bar + 2), y1 = y(value), yb = y(0), r = Math.min(4, bar / 2, (yb - y1) / 2);
        node.append(s("path", { class: `bar ${series[k].cls}`, d: `M${x} ${yb}V${y1 + r}Q${x} ${y1} ${x + r} ${y1}H${x + bar - r}Q${x + bar} ${y1} ${x + bar} ${y1 + r}V${yb}Z` }));
      });
      if (g.mark) node.append(s("path", { class: "warn-mark", d: `M${cx} ${y(Math.max(...g.values.map((v) => v || 0))) - 12}l4 4l-4 4l-4 -4Z` }));
      svg.append(node);
      if (i % every === (groups.length - 1) % every) svg.append(s("text", { class: "tick", x: cx, y: H - 12, "text-anchor": "middle" }, g.label));
    });
    svg.append(s("line", { class: "axis-line", x1: M.left, x2: W - M.right, y1: y(0), y2: y(0) }));
    chart.append(svg, tip);
    wrap.append(chart);
    const keys = series.length > 1 ? series.map((item) => h("li", null, h("span", { class: `swatch bar-key ${item.cls}` }), item.name)) : [];
    if (groups.some((g) => g.mark)) keys.push(h("li", null, h("span", { class: "swatch warn-key" }), "특보 구간을 지난 열차가 있던 날"));
    if (keys.length) wrap.append(h("ul", { class: "legend" }, keys));
    wrap.append(h("details", { class: "data-table" }, h("summary", { text: "표로 보기" }), h("div", { class: "table-scroll" }, h("table", null,
      h("thead", null, h("tr", null, tableHead.map((t) => h("th", { scope: "col", text: t })))),
      h("tbody", null, groups.map((g) => h("tr", null, tableRow(g).map((cell, i) => (i ? h("td", { text: cell }) : h("th", { scope: "row", text: cell }))))))))));
    return wrap;
  }

  // ------------------------------------------------------------ 날짜별 추이

  function renderDaily(r) {
    const box = clear($("daily"));
    if (!r.days.length || !r.all.n) { box.append(h("p", { class: "empty", text: "이 조건에 맞는 운행 기록이 없습니다." })); return; }
    const short = (ymd) => { const [, m, d] = ymd.split("-"); return `${Number(m)}.${Number(d)}`; };
    const groups = r.days.map((d) => {
      const a = stats(d.all), w = stats(d.warn);
      return { d, a, w, label: short(d.date), title: fmtDate(d.date) + (d.final ? "" : " (확정 전)"), values: [a.n ? a.mean : 0], mark: w.n > 0, picked: state.day === d.date, onPick: () => pickDay(d.date) };
    });
    box.append(barChart({
      groups, series: [{ name: "평균 종착 지연", cls: "s-main" }], unit: "분",
      label: `${scopeText(r)} — 날짜별 평균 종착 지연(분) 막대 그래프. 막대를 고르면 그날의 열차를 아래에서 볼 수 있습니다.`,
      tipRows: (g) => [["평균 지연", g.a.n ? `${mins(g.a.mean)}분` : "–"], ["열차", `${count(g.a.n)}편`], ["10분 이상", `${count(g.a.late10)}편`], ["특보 구간 통과", g.w.n ? `${count(g.w.n)}편 · 평균 ${mins(g.w.mean)}분` : "없음"],
        ["운행률(계획 대비)", rate(g.d.planned, g.d.noRecord)], ["열차 수(평소 대비)", versus(g.d.recorded, g.d.usual)]],
      tableHead: ["날짜", "열차 수", "평균(분)", "10분 이상", "최대(분)", "특보 구간 통과", "운행률(계획 대비)", "열차 수(평소 대비)"],
      tableRow: (g) => [g.title, count(g.a.n), g.a.n ? mins(g.a.mean) : "–", count(g.a.late10), String(g.a.max), g.w.n ? `${count(g.w.n)}편` : "–", rate(g.d.planned, g.d.noRecord), versus(g.d.recorded, g.d.usual)],
    }), h("p", { class: "note", text: "그날 조건에 맞는 모든 열차의 평균입니다. 막대를 누르면 그날의 열차 목록(⑧)으로 갑니다." }));
  }

  // ------------------------------------------------------------ 특보 종류별 · 지연 분포 · 노선별

  function renderByType(r) {
    const box = clear($("by-type")), rest = stats(r.clear);
    const rows = [...r.byName].map(([name, acc]) => ({ name, st: stats(acc) })).sort((a, b) => b.st.mean - a.st.mean);
    if (!rows.length) { box.append(h("p", { class: "empty", text: "이 조건에는 특보 구간을 지난 열차가 없습니다." })); return; }
    const max = Math.max(rest.mean || 0, ...rows.map((x) => x.st.mean), 1);
    const line = (name, st, cls, small) => h("li", { class: cls === "s-base" ? "rank-row base" : "rank-row" },
      h("div", { class: "rank-name" }, h("b", null, name), h("small", null, `${count(st.n)}편 · 10분 이상 ${pct(st.late10, st.n)}${small ? ` · ${small}` : ""}`)),
      h("div", { class: "rank-value" }, mins(st.mean), h("small", null, "분")),
      h("div", { class: "rank-track", "aria-hidden": "true" }, h("span", { class: `hbar ${cls}`, style: `width:${Math.max(0.5, (st.mean / max) * 100).toFixed(1)}%` })));
    put(box, h("p", { class: "rank-cap", text: "평균 종착 지연 — 막대가 길수록 더 늦게 도착했습니다" }),
      h("ul", { class: "rank" }, rest.n ? line("특보 없던 열차", rest, "s-base", "비교 기준") : null, rows.map((x) => line(x.name, x.st, "s-main", x.st.n < MIN_SAMPLE ? "표본 적음" : null))),
      h("p", { class: "note", text: "한 열차가 두 가지 특보 구간을 지나면 양쪽에 모두 셉니다. 30편 미만은 우연의 영향이 큽니다." }));
  }

  function renderDist(r) {
    const box = clear($("dist")), warn = stats(r.warn), rest = stats(r.clear), bins = state.cube.bins;
    if (!warn.n && !rest.n) { box.append(h("p", { class: "empty", text: "이 조건에 맞는 운행 기록이 없습니다." })); return; }
    const name = (i) => (i === bins.length - 1 ? `${bins[i]}분 이상` : bins[i + 1] - bins[i] === 1 ? `${bins[i]}분` : `${bins[i]}~${bins[i + 1] - 1}분`);
    const share = (st, i) => (st.n ? (st.hist[i] / st.n) * 100 : 0);
    const series = [{ name: "특보 구간을 지난 열차", cls: "s-main" }, { name: "특보 없던 열차", cls: "s-base" }];
    const groups = bins.map((_, i) => ({ i, label: name(i).replace("분 이상", "+").replace("분", ""), title: `종착 지연 ${name(i)}`, values: [share(warn, i), share(rest, i)] }));
    box.append(barChart({
      groups, series, unit: "%", tickEvery: 1,
      label: `${scopeText(r)} — 종착 지연 구간별 열차 비율(%). 특보 구간을 지난 열차와 특보 없던 열차의 비교.`,
      tipRows: (g) => [[series[0].name, warn.n ? `${num(share(warn, g.i), 0)}% (${count(warn.hist[g.i])}편)` : "–", "s-main"], [series[1].name, rest.n ? `${num(share(rest, g.i), 0)}% (${count(rest.hist[g.i])}편)` : "–", "s-base"]],
      tableHead: ["종착 지연", "특보 구간 통과", "특보 없던 열차"],
      tableRow: (g) => [name(g.i), warn.n ? `${num(share(warn, g.i), 0)}% (${count(warn.hist[g.i])}편)` : "–", rest.n ? `${num(share(rest, g.i), 0)}% (${count(rest.hist[g.i])}편)` : "–"],
    }), h("p", { class: "note", text: "가로축은 종착 지연(분) 구간, 세로축은 그 구간에 든 열차의 비율입니다. 일찍 도착한 열차는 0분으로 셉니다." }));
  }

  function renderByLine(r) {
    const box = clear($("by-line"));
    const rows = [...r.byLine].map(([name, v]) => ({ name, warn: stats(v.warn), clear: stats(v.clear) })).filter((x) => x.warn.n || x.clear.n)
      .sort((a, b) => b.warn.n - a.warn.n || b.clear.n - a.clear.n);
    if (!rows.length) { box.append(h("p", { class: "empty", text: "이 조건에 맞는 운행 기록이 없습니다." })); return; }
    const row = (x) => {
      const diff = x.warn.n && x.clear.n ? x.warn.mean - x.clear.mean : null;
      return h("tr", null, h("th", { scope: "row" }, h("button", { class: "linklike", type: "button", onclick: () => { state.filter.line = x.name; update(); $("h-filter").scrollIntoView({ behavior: "smooth", block: "start" }); } }, x.name)),
        h("td", null, x.warn.n ? `${mins(x.warn.mean)}분` : "–", h("small", null, x.warn.n ? `${count(x.warn.n)}편` : "없음")),
        h("td", null, x.clear.n ? `${mins(x.clear.mean)}분` : "–", h("small", null, x.clear.n ? `${count(x.clear.n)}편` : "없음")),
        h("td", { class: diff != null && diff >= MIN_EFFECT && x.warn.n >= MIN_SAMPLE ? "over" : null }, diff == null ? "–" : `${smins(diff)}분`, h("small", null, !x.warn.n ? " " : x.warn.n < MIN_SAMPLE ? "표본 적음" : " ")));
    };
    const head = h("thead", null, h("tr", null, h("th", { scope: "col", text: "노선" }), h("th", { scope: "col" }, "특보 구간 통과", h("small", null, "평균 지연")), h("th", { scope: "col" }, "특보 없던 열차", h("small", null, "평균 지연")), h("th", { scope: "col", text: "차이" })));
    put(box, h("div", { class: "table-scroll" }, h("table", { class: "fit line-table" }, head, h("tbody", null, rows.slice(0, 12).map(row)))),
      rows.length > 12 ? h("details", { class: "data-table" }, h("summary", { text: `나머지 ${rows.length - 12}개 노선 보기` }), h("div", { class: "table-scroll" }, h("table", { class: "fit line-table" }, h("tbody", null, rows.slice(12).map(row))))) : null,
      h("p", { class: "note", text: "노선 이름을 누르면 그 노선만 골라 봅니다. 노선은 열차의 주운행선 기준입니다(전라선 KTX 는 고속선 구간을 달려도 전라선)." }));
  }

  // ------------------------------------------------------------ 특보가 있었던 날 · 그날의 열차

  function renderDays(r) {
    const box = clear($("days")), rows = r.days.filter((d) => d.warn.n > 0).reverse();
    if (!rows.length) { box.append(h("p", { class: "empty", text: "이 조건에는 특보 구간을 지난 열차가 있던 날이 없습니다." })); return; }
    box.append(h("div", { class: "table-scroll" }, h("table", { class: "fit days-table" },
      h("thead", null, h("tr", null, h("th", { scope: "col" }, "날짜", h("small", null, "지나간 특보 · 열차 수")), h("th", { scope: "col" }, "평균 지연", h("small", null, "특보 구간 통과")), h("th", { scope: "col" }, "운행률", h("small", null, "계획 대비 · 평소 대비")))),
      h("tbody", null, rows.map((d) => {
        const w = stats(d.warn), c = stats(d.clear);
        return h("tr", { class: state.day === d.date ? "picked-row" : null },
          h("th", { scope: "row" }, h("button", { class: "linklike", type: "button", onclick: () => pickDay(d.date) }, fmtDate(d.date)), d.final ? null : h("small", { class: "tag", text: "확정 전" }),
            h("div", { class: "day-pills" }, [...d.names].sort((a, b) => b[1] - a[1]).map(([name, n]) => h("span", { class: `pill ${/경보$/.test(name) ? "warn" : "adv"}`, text: `${name} ${count(n)}편` })))),
          h("td", null, `${mins(w.mean)}분`, h("small", null, c.n ? `그 외 ${mins(c.mean)}분` : "그 외 없음")),
          h("td", { class: d.usual && d.recorded / d.usual < 0.97 ? "over" : null }, rate(d.planned, d.noRecord), h("small", null, d.usual ? `평소의 ${versus(d.recorded, d.usual)}` : d.final ? "평소 값 없음" : "확정 뒤 계산"),
            d.noRecord ? h("small", null, `기록 없음 ${count(d.noRecord)}편`) : null));
      })))), h("p", { class: "note", text: `${rows.length}일 · 날짜를 누르면 그날의 열차 목록(⑧)을 봅니다. 운행률은 그날 전체 열차 기준입니다. ‘평소의 ○%’는 같은 요일의 평소 운행 열차 수와 견준 값, ‘기록 없음’은 계획에는 있으나 운행 기록이 없는 열차입니다.` }));
  }

  function pickDay(date) {
    state.day = date; state.showAll = false;
    update();
    $("h-day").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function renderDay() {
    const box = $("day-detail"), date = state.day;
    if (!date) { clear(box).append(h("div", { class: "panel" }, h("p", { class: "empty", text: "위의 날짜별 그래프나 ‘특보가 있었던 날’에서 날짜를 고르면 그날의 열차가 나옵니다." }))); $("day-hint").textContent = ""; return; }
    if (!state.days[date]) {
      clear(box).append(h("div", { class: "panel" }, h("p", { class: "empty", text: "그날의 열차 목록을 불러오는 중…" })));
      state.days[date] = await getJson(`data/rail-days/${date}.json`).catch(() => "failed");
      if (state.day !== date) return;
    }
    const doc = state.days[date];
    clear(box);
    if (doc === "failed") { delete state.days[date]; box.append(h("div", { class: "panel" }, h("p", { class: "empty", text: "그날의 열차 목록을 불러오지 못했습니다." }))); return; }
    const f = state.filter, idx = Object.fromEntries(doc.fields.map((name, i) => [name, i])), q = state.query.trim().replace(/\s+/g, "");
    const rows = doc.trains.map((t) => ({ no: t[idx.trn_no], line: t[idx.line] || "미상", cls: t[idx.class], origin: stationLabel(t[idx.origin] || ""), dest: stationLabel(t[idx.dest] || ""), plan: t[idx.plan_arr], delay: t[idx.arr_delay], warnings: t[idx.warnings] || [] }))
      .filter((t) => (f.line === "all" || t.line === f.line) && (f.cls === "all" || t.cls === f.cls))
      .filter((t) => !state.warnedOnly || t.warnings.some((name) => f.type === "all" || typeOf(name) === f.type))
      .filter((t) => !q || `${Number(t.no)}${t.no}${t.cls}${t.line}${t.origin}${t.dest}${t.warnings.join("")}`.replace(/\s+/g, "").includes(q))
      .sort((a, b) => (b.delay ?? -1) - (a.delay ?? -1));
    $("day-hint").textContent = `${fmtDate(date)} · ${f.line === "all" ? "모든 노선" : f.line} · ${f.cls === "all" ? "모든 열차" : f.cls}${doc.final === false ? " · 확정 전(자료가 더 올라올 수 있음)" : ""}`;
    const search = h("input", { type: "search", id: "day-search", class: "search", placeholder: "열차번호·역·노선 찾기 (예: 501, 순천)", value: state.query, "aria-label": "그날의 열차에서 찾기",
      oninput: (event) => { state.query = event.target.value; state.showAll = false; renderDay().then(() => { const input = $("day-search"); if (input) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); } }); } });
    const only = h("button", { class: "chip", type: "button", "aria-pressed": String(state.warnedOnly), onclick: () => { state.warnedOnly = !state.warnedOnly; state.showAll = false; renderDay(); } }, "특보 구간 통과만");
    const limit = state.showAll ? rows.length : 30;
    box.append(h("div", { class: "panel" }, h("div", { class: "search-row" }, search, only),
      rows.length ? h("div", { class: "table-scroll" }, h("table", { class: "fit" },
        h("thead", null, h("tr", null, h("th", { scope: "col" }, "열차", h("small", null, "구간 · 계획 도착")), h("th", { scope: "col", text: "지연(분)" }), h("th", { scope: "col", text: "지나간 특보" }))),
        h("tbody", null, rows.slice(0, limit).map((t) => h("tr", null,
          h("th", { scope: "row" }, `${t.cls} ${Number(t.no)}`, h("small", null, `${t.line} · ${t.origin} → ${t.dest}${t.plan ? ` · ${t.plan} 도착` : ""}`)),
          h("td", { class: t.delay >= 10 ? "over" : null, text: t.delay == null ? "기록 없음" : String(Math.max(0, t.delay)) }),
          h("td", { text: t.warnings.join(", ") || "–" })))))) : h("p", { class: "empty", text: "조건에 맞는 열차가 없습니다." }),
      rows.length > limit ? h("button", { class: "chip more", type: "button", onclick: () => { state.showAll = true; renderDay(); } }, `나머지 ${count(rows.length - limit)}편 더 보기`) : null,
      h("p", { class: "note", text: `${count(rows.length)}편 · 지연이 큰 순서. 지연은 종착역 도착 기준이며 일찍 도착하면 0으로 적습니다. ‘기록 없음’은 계획에는 있으나 운행 기록이 없는 열차입니다.` })));
  }

  // ------------------------------------------------------------ 예측 적중률

  function renderAccuracy() {
    const acc = state.accuracy, box = clear($("accuracy")), period = $("accuracy-period");
    if (!acc) { period.textContent = ""; box.append(h("div", { class: "panel" }, h("p", { class: "empty", text: "적중률 자료를 불러오지 못했습니다." }))); return; }
    const p = acc.period || {};
    period.textContent = p.from ? `${fmtClock(p.from)} ~ ${fmtClock(p.to)} · ${p.runs}회 수집 · 실제 특보 발효 ${acc.events}건` : "아직 수집 이력이 없습니다.";
    const tile = (label, stat, hitKey, totalKey, unitWord, emptyWord) => {
      const total = stat ? stat[totalKey] : 0;
      const value = total ? h("p", { class: "value" }, String(Math.round(stat.rate * 100)), h("small", null, "%")) : h("p", { class: "value pending", text: "집계 중" });
      return h("div", { class: "tile" }, h("p", { class: "label", text: label }), value, h("p", { class: "sub", text: total ? `${total}${unitWord} 중 ${stat[hitKey]}건` : emptyWord }));
    };
    const tiles = h("div", { class: "tiles" });
    for (const lead of acc.lead_hours || []) {
      tiles.append(tile(`사전 포착률 · ${lead}시간 전`, (acc.capture || {})[String(lead)], "captured", "events", "건의 특보", "검증할 특보 발효 사례가 아직 없습니다"));
    }
    tiles.append(tile("오경보율 · ‘주의’ 이상", (acc.false_alarm || {}).caution_or_higher, "false", "alerts", "회 예측", "검증할 예측이 아직 없습니다"));
    tiles.append(tile("오경보율 · ‘높음’", (acc.false_alarm || {}).high, "false", "alerts", "회 예측", "검증할 예측이 아직 없습니다"));
    box.append(tiles, h("p", { class: "note" },
      `사전 포착률: 실제로 발효된 특보 가운데, 발효 12·24시간 전에 ‘주의’ 이상으로 표시했던 비율. 오경보율: ‘주의’ 이상으로 표시한 예측 가운데, 이후 ${acc.horizon_hours}시간 안에 실제 특보가 없었던 비율(같은 상황을 3시간마다 다시 세므로 건수는 예측 횟수 기준). 기상청 발표를 반영하기 전의 계산 등급으로 비교합니다. 표본이 적은 동안에는 참고만 하세요.`));
  }

  // ------------------------------------------------------------ 그리기·시작

  /** 조건이 바뀔 때마다: 한 번 다시 세고, 모든 구역을 그 결과로 그린다. */
  function update() {
    const cube = state.cube;
    if (!cube || !cube.days.length) return;
    renderFilters();
    const r = compute();
    if (state.day && (state.day < r.from || state.day > r.to)) state.day = null;
    const scope = scopeText(r);
    $("scope").textContent = `${scope} — 열차 ${count(r.all.n)}편`;
    for (const id of ["scope-kpi", "scope-daily", "scope-type", "scope-dist", "scope-line", "scope-days"]) $(id).textContent = scope;
    renderKpis(r); renderDaily(r); renderByType(r); renderDist(r); renderByLine(r); renderDays(r); renderDay();
  }

  function renderHead() {
    const cube = state.cube, alerts = clear($("alerts"));
    if ((state.latest && state.latest.demo) || (cube && cube.demo)) alerts.append(h("div", { class: "banner demo", role: "status" }, h("div", null, "데모 화면입니다. 표시된 내용은 실제가 아닌 가상 자료입니다.")));
    // 운행 기록이 아직 없으면 조건·그래프 구역은 통째로 감추고 안내 한 줄만 남긴다(빈 상자를 늘어놓지 않는다)
    const ready = Boolean(cube && cube.days.length);
    for (const el of document.querySelectorAll("[data-needs-cube]")) el.hidden = !ready;
    $("kpis").hidden = !ready; $("operation").hidden = !ready;
    if (!ready) {
      $("updated").textContent = ""; $("scope-kpi").textContent = "";
      clear($("verdict")).append(h("p", { class: "empty", text: "아직 집계된 열차 운행 기록이 없습니다. 수집이 시작되면 표시됩니다." }));
      return;
    }
    const last = cube.days[cube.days.length - 1].date;
    $("updated").textContent = `운행 기록 ${fmtDate(cube.days[0].date)} ~ ${fmtDate(last)} · ${cube.days.length}일`;
  }

  async function load() {
    const [cube, accuracy, latest] = await Promise.all([getJson("data/rail-cube.json").catch(() => null), getJson("data/accuracy.json").catch(() => null), getJson("data/latest.json").catch(() => null)]);
    const changed = Boolean(cube) && (!state.cube || JSON.stringify(cube) !== JSON.stringify(state.cube));  // 못 받아 온 때는 보던 화면을 그대로 둔다
    state.cube = cube || state.cube;
    state.accuracy = accuracy || state.accuracy;
    state.latest = latest || state.latest;
    renderHead();
    if (changed) update();
    renderAccuracy();
  }

  $("f-line").addEventListener("change", (event) => { state.filter.line = event.target.value; update(); });
  $("f-class").addEventListener("change", (event) => { state.filter.cls = event.target.value; update(); });
  for (const [id, key] of [["f-from", "from"], ["f-to", "to"]]) $(id).addEventListener("change", (event) => { if (event.target.value) { state.filter[key] = event.target.value; update(); } });
  $("reset").addEventListener("click", () => { state.filter = { period: "all", from: null, to: null, type: "all", line: "all", cls: "all" }; update(); });
  load();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
  setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
  let resizeTimer = 0, lastWidth = $("main").clientWidth;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if ($("main").clientWidth !== lastWidth) { lastWidth = $("main").clientWidth; update(); } }, 200);
  });
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => { /* 오프라인 지원 없이 계속 */ }));
  }
})();
