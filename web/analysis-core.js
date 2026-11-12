/* 분석 화면의 계산 부분 — 묶음 자료(data/rail-cube.json)를 조건에 맞춰 다시 센다. 화면(DOM)을 쓰지 않는다.
 *
 * 묶음 한 줄(cell): [노선 번호, 종류 번호, [특보 번호…], 열차 수, 지연 합, 지연 제곱합, 최대 지연, 구간별 열차 수 × 7]
 * 판정 규칙은 서버(teukbo.rail.adjusted_difference)와 같아야 한다 — tests/test_rail.py 가 두 결과를 대조한다.
 */
window.TeukboAnalysisCore = (() => {
  "use strict";
  const MIN_SAMPLE = 30, MIN_EFFECT = 1.0;  // 이보다 적은 열차는 '자료 부족', 이보다 작은 차이는 '차이 없음'
  const MIN_BASE_DAYS = 3;                  // 같은 요일이 이만큼은 있어야 '평소 열차 수'를 낸다
  const weekday = (ymd) => new Date(`${ymd}T00:00:00Z`).getUTCDay();
  const typeOf = (name) => name.replace(/(중대경보|주의보|경보)$/, "");
  const addDays = (ymd, delta) => { const t = new Date(`${ymd}T00:00:00Z`); t.setUTCDate(t.getUTCDate() + delta); return t.toISOString().slice(0, 10); };

  const blank = () => ({ n: 0, sum: 0, sq: 0, max: 0, hist: [0, 0, 0, 0, 0, 0, 0] });
  function add(acc, cell) {
    acc.n += cell[3]; acc.sum += cell[4]; acc.sq += cell[5]; acc.max = Math.max(acc.max, cell[6]);
    for (let i = 0; i < 7; i++) acc.hist[i] += cell[7 + i];
  }
  /** 묶음 → 통계. late5·late10·late20 = 5·10·20분 이상 늦은 열차 수 */
  function stats(acc) {
    const tail = (from) => acc.hist.slice(from).reduce((a, b) => a + b, 0);
    return { ...acc, mean: acc.n ? acc.sum / acc.n : null, late5: tail(2), late10: tail(3), late20: tail(4) };
  }

  /** 같은 노선끼리 비교한 차이(특보 구간을 지난 열차 − 그 외). 규칙은 teukbo.rail.adjusted_difference 와 같다. */
  function adjusted(byLine) {
    let total = 0, lines = 0, weighted = 0;
    const terms = [];
    for (const { warn, clear } of byLine.values()) {
      if (!warn.n || clear.n < 2) continue;
      const mw = warn.sum / warn.n, mc = clear.sum / clear.n;
      const vc = (clear.sq - clear.n * mc * mc) / (clear.n - 1);
      const vw = warn.n > 1 ? (warn.sq - warn.n * mw * mw) / (warn.n - 1) : vc;
      lines += 1; total += warn.n; weighted += warn.n * (mw - mc);
      terms.push([warn.n, Math.max(0, vw) / warn.n + Math.max(0, vc) / clear.n]);
    }
    if (!total) return { n: 0, lines: 0, verdict: "insufficient" };
    const diff = weighted / total, se = Math.sqrt(terms.reduce((a, [n, v]) => a + (n / total) ** 2 * v, 0));
    const low = diff - 1.96 * se, high = diff + 1.96 * se;
    const verdict = total < MIN_SAMPLE ? "insufficient" : low > 0 && diff >= MIN_EFFECT ? "later" : high < 0 && diff <= -MIN_EFFECT ? "earlier" : "no_difference";
    const r1 = (v) => Math.round(v * 10) / 10;
    return { n: total, lines, diff: r1(diff), low: r1(low), high: r1(high), verdict };
  }

  /** 조건의 기간(시작·끝 날짜). 자료가 있는 범위 안으로 맞춘다. period: "7" | "30" | "all" | "custom"(from·to 사용) */
  function range(cube, f) {
    const days = cube.days, first = days[0].date, last = days[days.length - 1].date;
    if (f.period === "custom") {
      const from = f.from && f.from >= first ? f.from : first, to = f.to && f.to <= last ? f.to : last;
      return from <= to ? { from, to } : { from: to, to: from };
    }
    return { from: f.period === "all" ? first : [addDays(last, 1 - Number(f.period)), first].sort()[1], to: last };
  }

  /** 요일별 '평소 운행 열차 수' = 확정된 날들 가운데 같은 요일의 중앙값. 돌려주는 함수: 날짜 → 평소 열차 수(모자라면 null).
   *  운휴 열차가 계획 자료에서 아예 빠져 '계획 대비 운행률'이 100% 로 보이는 날도, 평소보다 열차가 줄어든 것으로 드러난다. */
  function usualRuns(cube) {
    const by = new Map(), base = new Map();
    for (const day of cube.days) {
      if (day.final === false) continue;
      const wd = weekday(day.date);
      if (!by.has(wd)) by.set(wd, []);
      by.get(wd).push(day.recorded);
    }
    for (const [wd, list] of by) {
      if (list.length < MIN_BASE_DAYS) continue;
      list.sort((a, b) => a - b);
      const mid = list.length >> 1;
      base.set(wd, list.length % 2 ? list[mid] : (list[mid - 1] + list[mid]) / 2);
    }
    return (ymd) => base.get(weekday(ymd)) ?? null;
  }

  /** 여러 날의 운행 합계. planned·noRecord → 계획 대비 운행률, ran·usual → 평소 대비 열차 수(평소 값을 낼 수 있는 날만). */
  function operation(days) {
    const out = { days: days.length, planned: 0, noRecord: 0, ran: 0, usual: 0 };
    for (const d of days) {
      out.planned += d.planned; out.noRecord += d.noRecord;
      if (d.usual != null) { out.ran += d.recorded; out.usual += d.usual; }
    }
    return out;
  }

  /** 조건에 맞춰 다시 센 결과 전체. 화면의 모든 구역이 이 하나를 쓴다. */
  function compute(cube, filter) {
    const { from, to } = range(cube, filter);
    const names = cube.warnings, wanted = (name) => filter.type === "all" || typeOf(name) === filter.type, usual = usualRuns(cube);
    const out = { from, to, days: [], all: blank(), warn: blank(), clear: blank(), planned: 0, noRecord: 0, byName: new Map(), byLine: new Map() };
    for (const day of cube.days) {
      if (day.date < from || day.date > to) continue;
      const d = { date: day.date, final: day.final, planned: day.planned, noRecord: day.no_record, recorded: day.recorded, usual: day.final === false ? null : usual(day.date),
        all: blank(), warn: blank(), clear: blank(), names: new Map() };
      out.planned += day.planned; out.noRecord += day.no_record;
      for (const cell of day.cells) {
        const line = cube.lines[cell[0]];
        if ((filter.line !== "all" && line !== filter.line) || (filter.cls !== "all" && cube.classes[cell[1]] !== filter.cls)) continue;
        const hit = cell[2].map((i) => names[i]).filter(wanted);
        const group = !cell[2].length ? "clear" : hit.length ? "warn" : null;  // 다른 종류의 특보만 지난 열차는 어느 쪽에도 넣지 않는다
        add(out.all, cell); add(d.all, cell);
        if (!group) continue;
        add(out[group], cell); add(d[group], cell);
        if (!out.byLine.has(line)) out.byLine.set(line, { warn: blank(), clear: blank() });
        add(out.byLine.get(line)[group], cell);
        for (const name of hit) {
          if (!out.byName.has(name)) out.byName.set(name, blank());
          add(out.byName.get(name), cell);
          d.names.set(name, (d.names.get(name) || 0) + cell[3]);
        }
      }
      out.days.push(d);
    }
    out.adjusted = adjusted(out.byLine);
    out.hitDays = operation(out.days.filter((d) => d.warn.n > 0));   // 조건의 특보 구간을 지난 열차가 있던 날
    out.calmDays = operation(out.days.filter((d) => !d.warn.n));
    return out;
  }

  return { MIN_SAMPLE, MIN_EFFECT, MIN_BASE_DAYS, typeOf, blank, add, stats, adjusted, range, usualRuns, operation, compute };
})();
