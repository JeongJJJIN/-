/* 분석 화면의 계산 부분 — 묶음 자료(data/rail-cube.json)를 조건에 맞춰 다시 센다. 화면(DOM)을 쓰지 않는다.
 *
 * 묶음 한 줄(cell): [노선 번호, 종류 번호, [특보 번호…], 열차 수, 지연 합, 지연 제곱합, 최대 지연, 구간별 열차 수 × 7,
 *                    평소 값을 낼 수 있었던 열차 수, (지연 − 그 열차의 평소) 합]   ← 마지막 둘은 특보 구간을 지난 열차에만
 * 판정 규칙은 서버(teukbo.rail.matched_difference)와 같아야 한다 — tests/test_rail.py 가 두 결과를 대조한다.
 */
window.TeukboAnalysisCore = (() => {
  "use strict";
  const MIN_SAMPLE = 30, MIN_EFFECT = 1.0;  // 이보다 적은 열차는 판정하지 않는다 · 이보다 작은 차이는 운영상 의미가 없다고 본다
  const MIN_DAYS = 8, MAX_DAY_SHARE = 0.4;  // 특보일이 이보다 적거나, 하루에 이 비율 넘게 몰려 있으면 판정하지 않는다
  const NOTE_DAYS = 4;                      // 특보일이 이보다 적으면 평균도 내보이지 않는다(날짜별 목록만)
  const HIT_SHARE = 0.05;                   // 그날 열차의 이 비율 이상이 특보 구간을 지났을 때 '특보가 걸린 날'로 본다(운행 비교)
  const T95 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
    2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042];
  const t95 = (df) => (df >= 1 && df <= T95.length ? T95[df - 1] : 1.96 + 2.4 / df);
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

  /** 특보 구간을 지난 열차가 '같은 열차의 평소'보다 얼마나 늦었나 — 날짜를 단위로 본 평균과 오차 범위.
   *  pairs: 날마다 [평소 값을 낼 수 있었던 열차 수, (지연 − 평소) 합]. 규칙과 까닭은 teukbo.rail.matched_difference 에 적혀 있다. */
  function matched(pairs) {
    const used = pairs.filter(([n]) => n > 0), trains = used.reduce((a, [n]) => a + n, 0), days = used.length;
    if (!trains) return { n: 0, days: 0, verdict: "hold", reason: "few_trains" };
    const mean = used.reduce((a, [, total]) => a + total, 0) / trains, top = Math.max(...used.map(([n]) => n)) / trains;
    const half_up = (v, scale) => Math.floor(v * scale + 0.5) / scale + 0;  // 서버(_round_half_up)와 같은 반올림. + 0: -0 을 0 으로
    const r1 = (v) => half_up(v, 10);
    const out = { n: trains, days, top_share: half_up(top, 100), diff: r1(mean) };
    if (days >= 2) {
      const half = t95(days - 1) * Math.sqrt((days / (days - 1)) * used.reduce((a, [n, total]) => a + (total - mean * n) ** 2, 0)) / trains;
      out.low = r1(mean - half); out.high = r1(mean + half);
    }
    if (trains < MIN_SAMPLE) return { ...out, verdict: "hold", reason: "few_trains" };
    if (days < MIN_DAYS) return { ...out, verdict: "hold", reason: "few_days" };
    if (top > MAX_DAY_SHARE) return { ...out, verdict: "hold", reason: "one_day" };
    if (out.low >= -MIN_EFFECT && out.high <= MIN_EFFECT) return { ...out, verdict: "within" };
    if (out.low > 0) return { ...out, verdict: "later" };
    if (out.high < 0) return { ...out, verdict: "earlier" };
    return { ...out, verdict: "hold", reason: "wide" };
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

  /** 조건에 맞춰 다시 센 결과 전체. 화면의 모든 구역이 이 하나를 쓴다.
   *  warn = 조건의 특보 구간을 지난 열차, clear = 어떤 특보 구간도 지나지 않은 열차. 다른 종류의 특보만 지난 열차는 어느 쪽에도 넣지 않는다.
   *  byName·byLine 의 값: { acc(또는 warn·clear): 묶음, days: Map(날짜 → [평소 값 있는 열차 수, 평소 대비 합]), matched: 판정 } */
  function compute(cube, filter) {
    const { from, to } = range(cube, filter);
    const names = cube.warnings, wanted = (name) => filter.type === "all" || typeOf(name) === filter.type, usual = usualRuns(cube);
    const out = { from, to, days: [], all: blank(), warn: blank(), clear: blank(), planned: 0, noRecord: 0, byName: new Map(), byLine: new Map() };
    const bump = (map, date, cell) => { const pair = map.get(date) || [0, 0]; pair[0] += cell[14] || 0; pair[1] += cell[15] || 0; map.set(date, pair); };
    for (const day of cube.days) {
      if (day.date < from || day.date > to) continue;
      const d = { date: day.date, final: day.final, planned: day.planned, noRecord: day.no_record, recorded: day.recorded, usual: day.final === false ? null : usual(day.date),
        all: blank(), warn: blank(), clear: blank(), names: new Map(), pair: [0, 0] };
      out.planned += day.planned; out.noRecord += day.no_record;
      for (const cell of day.cells) {
        const line = cube.lines[cell[0]];
        if ((filter.line !== "all" && line !== filter.line) || (filter.cls !== "all" && cube.classes[cell[1]] !== filter.cls)) continue;
        const hit = cell[2].map((i) => names[i]).filter(wanted);
        const group = !cell[2].length ? "clear" : hit.length ? "warn" : null;
        add(out.all, cell); add(d.all, cell);
        if (!group) continue;
        add(out[group], cell); add(d[group], cell);
        if (!out.byLine.has(line)) out.byLine.set(line, { warn: blank(), clear: blank(), days: new Map() });
        add(out.byLine.get(line)[group], cell);
        if (group !== "warn") continue;
        d.pair[0] += cell[14] || 0; d.pair[1] += cell[15] || 0;
        bump(out.byLine.get(line).days, day.date, cell);
        for (const name of hit) {
          if (!out.byName.has(name)) out.byName.set(name, { acc: blank(), days: new Map() });
          add(out.byName.get(name).acc, cell);
          bump(out.byName.get(name).days, day.date, cell);
          d.names.set(name, (d.names.get(name) || 0) + cell[3]);
        }
      }
      out.days.push(d);
    }
    out.matched = matched(out.days.map((d) => d.pair));
    out.warnDays = out.days.filter((d) => d.warn.n > 0).length;  // 특보일: 조건의 특보 구간을 지난 열차가 있던 날
    for (const group of [out.byName, out.byLine]) for (const value of group.values()) { value.matched = matched([...value.days.values()]); value.warnDays = value.days.size; }
    // 운행 비교: 그날 조건에 맞는 열차의 HIT_SHARE 이상이 특보 구간을 지난 날 / 그 밖의 날
    const isHit = (d) => d.warn.n > 0 && d.warn.n >= HIT_SHARE * d.all.n;
    out.hitDays = operation(out.days.filter(isHit));
    out.calmDays = operation(out.days.filter((d) => !isHit(d)));
    return out;
  }

  return { MIN_SAMPLE, MIN_EFFECT, MIN_DAYS, MAX_DAY_SHARE, NOTE_DAYS, HIT_SHARE, MIN_BASE_DAYS, typeOf, blank, add, stats, matched, range, usualRuns, operation, compute };
})();
