/* 분석 — 이 서비스가 쌓은 자료로만 볼 수 있는 것.
 *  ① 열차 지연 × 특보: 코레일 공식 운행 기록(전날까지)과 기상청 특보 발표 이력을 묶은 통계 (data/rail.json)
 *  ② 예측 적중률: 3시간마다 기록한 '특보 가능성'을 실제 특보 발효와 대조 (data/accuracy.json)
 * 계산은 모두 서버(teukbo.rail, teukbo.accuracy)에서 끝나 있고 여기서는 보여 주기만 한다.
 */
(() => {
  "use strict";
  const { h, s, $, clear, fmtClock, fmtDate, fmtAgo, num, signed, share, stationLabel, getJson } = window.TeukboUI;
  const VERDICT = { later: "더 늦었음", earlier: "덜 늦었음", no_difference: "뚜렷한 차이 없음", insufficient: "자료 부족" };
  const REFRESH_MS = 30 * 60 * 1000;
  const state = { summary: null, accuracy: null, latest: null };

  // ------------------------------------------------------------ 핵심 요약

  /** 통계에서 바로 읽을 수 있는 사실만 문장으로 옮긴다(해석을 보태지 않는다). */
  function renderSummary() {
    const box = clear($("summary")), sum = state.summary, acc = state.accuracy, items = [];
    if (sum && sum.period && sum.period.days) {
      const adj = (sum.adjusted || {}).all || { verdict: "insufficient", n: 0 };
      const span = `${fmtDate(sum.period.from)}~${fmtDate(sum.period.to)}(${sum.period.days}일)`;
      const interval = adj.diff == null ? "" : `95% 구간 ${signed(adj.low)}~${signed(adj.high)}분`;
      const text = {
        later: `특보 구간을 지난 열차 ${adj.n}편은 같은 노선의 다른 열차보다 평균 ${num(adj.diff)}분 더 늦게 도착했습니다(${interval}).`,
        earlier: `특보 구간을 지난 열차 ${adj.n}편은 같은 노선의 다른 열차보다 오히려 평균 ${num(Math.abs(adj.diff))}분 덜 늦었습니다(${interval}).`,
        no_difference: `특보 구간을 지난 열차 ${adj.n}편과 같은 노선의 다른 열차 사이에 뚜렷한 지연 차이가 없었습니다(차이 ${signed(adj.diff)}분, ${interval}).`,
        insufficient: `특보 구간을 지난 열차가 ${adj.n || 0}편뿐이라 지연 차이를 판단하기에는 자료가 부족합니다.`,
      }[adj.verdict];
      items.push(h("li", null, h("b", null, `열차 지연 × 특보 · ${span}: `), text));
      const later = Object.entries((sum.adjusted || {}).by_type || {}).filter(([, v]) => v.verdict === "later").sort((a, b) => b[1].diff - a[1].diff);
      if (later.length) items.push(h("li", null, h("b", null, "더 늦었던 특보 종류: "), later.map(([name, v]) => `${name} ${signed(v.diff)}분(${v.n}편)`).join(", "), "."));
      const types = Object.keys(sum.by_type || {});
      if (types.length) items.push(h("li", null, h("b", null, "이 기간에 열차가 지난 특보: "), `${types.join("·")}. `, types.some((t) => ["호우", "대설", "태풍"].includes(t)) ? "" : "호우·대설·태풍 사례는 아직 없어, 그런 날의 영향은 자료가 쌓여야 알 수 있습니다."));
    } else {
      items.push(h("li", null, h("b", null, "열차 지연 × 특보: "), "아직 집계된 운행 기록이 없습니다."));
    }
    if (acc && acc.period && acc.period.from) {
      const lead = (acc.lead_hours || [])[0], cap = lead != null ? (acc.capture || {})[String(lead)] : null;
      const fa = (acc.false_alarm || {}).caution_or_higher;
      items.push(h("li", null, h("b", null, "예측 적중률: "),
        cap && cap.events ? `실제 발효된 특보 ${cap.events}건 가운데 ${Math.round(cap.rate * 100)}%를 ${lead}시간 전에 ‘주의’ 이상으로 미리 표시했습니다. ` : `수집을 시작한 뒤 대상 지역에 발효된 특보가 아직 없어 사전 포착률은 집계 중입니다(${acc.period.runs}회 수집). `,
        fa && fa.alerts ? `‘주의’ 이상 예측 ${fa.alerts}회 중 ${Math.round(fa.rate * 100)}%는 이후 특보가 없었습니다(오경보).` : ""));
    }
    box.append(h("ul", { class: "findings" }, items),
      h("p", { class: "note", text: "위 문장은 집계 값을 그대로 옮긴 것입니다. ‘같은 때 있었다’는 사실이며 원인을 뜻하지 않습니다." }));
    const when = (sum && sum.updated_at) || (acc && acc.period && acc.period.to);
    $("updated").textContent = when ? `집계 ${fmtClock(when)} · ${fmtAgo(when)}` : "";
    const alerts = clear($("alerts"));
    if ((state.latest && state.latest.demo) || (sum && sum.demo)) alerts.append(h("div", { class: "banner demo", role: "status" }, h("div", null, "데모 화면입니다. 표시된 내용은 실제가 아닌 가상 자료입니다.")));
  }

  // ------------------------------------------------------------ 열차 지연 × 특보

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

    box.append(h("h3", { class: "sub-h", text: "같은 노선끼리 비교한 차이" }), h("div", { class: "panel" }, adjustedTable(sum),
      h("p", { class: "note", text: "노선마다 평소 지연이 달라서, 노선별로 ‘특보 구간을 지난 열차 − 지나지 않은 열차’의 평균 차이를 구한 뒤 열차 수로 가중 평균했습니다. 95% 구간이 0을 포함하면 우연한 차이일 수 있습니다. 1분 미만의 차이와 30편 미만의 표본은 판정하지 않습니다." })));

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

  /** 같은 노선끼리 비교한 차이 — 전체와 특보 종류별. */
  function adjustedTable(sum) {
    const adj = sum.adjusted || { all: { n: 0, verdict: "insufficient" }, by_type: {} };
    const row = (label, v, strong) => h("tr", { class: strong ? "strong" : null },
      h("th", { scope: "row", text: label }),
      h("td", { text: String(v.n || 0) }),
      h("td", { class: v.verdict === "later" ? "over" : null, text: v.diff == null ? "–" : `${signed(v.diff)}분` }),
      h("td", { text: v.diff == null ? "–" : `${signed(v.low)} ~ ${signed(v.high)}` }),
      h("td", null, h("span", { class: `pill verdict-${v.verdict}`, text: VERDICT[v.verdict] || v.verdict })));
    return h("div", { class: "table-scroll" }, h("table", { class: "fit" },
      h("thead", null, h("tr", null, h("th", { scope: "col", text: "특보" }), h("th", { scope: "col" }, "열차 수", h("small", null, "특보 구간 통과")), h("th", { scope: "col" }, "차이", h("small", null, "같은 노선 대비")), h("th", { scope: "col" }, "95% 구간", h("small", null, "분")), h("th", { scope: "col", text: "판정" }))),
      h("tbody", null, row("전체", adj.all, true), Object.entries(adj.by_type || {}).map(([name, v]) => row(`· ${name}`, v)))));
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

  // ------------------------------------------------------------ 시작

  async function load() {
    const [summary, accuracy, latest] = await Promise.all([
      getJson("data/rail.json").catch(() => null), getJson("data/accuracy.json").catch(() => null), getJson("data/latest.json").catch(() => null)]);
    state.summary = summary || state.summary;
    state.accuracy = accuracy || state.accuracy;
    state.latest = latest || state.latest;
    renderSummary();
    renderDelay();
    renderAccuracy();
  }

  load();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
  setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
  let resizeTimer = 0, lastWidth = $("delay").clientWidth;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if ($("delay").clientWidth !== lastWidth) { lastWidth = $("delay").clientWidth; renderDelay(); } }, 200);
  });
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => { /* 오프라인 지원 없이 계속 */ }));
  }
})();
