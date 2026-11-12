/* 실시간 자료 — 두 화면(특보 가능성, 철도 지도)이 함께 쓴다.
 *
 * live.json: 몇 분마다 새로 만들어지는 특보·예비특보, 지금 날씨(초단기실황), 앞으로 6시간(초단기예보).
 * 화면은 3시간마다 만들어지는 latest.json 위에 이 값을 덮어 보여 준다.
 * 원칙: 값마다 '확인 시각'을 따로 보고, 기준보다 오래된 값은 쓰지 않는다(지연을 숨기지 않는다).
 */
window.TeukboLive = (() => {
  "use strict";

  const RANK = { unknown: -1, low: 0, caution: 1, high: 2, active: 3 };
  const PTY = { 0: "", 1: "비", 2: "비/눈", 3: "눈", 5: "빗방울", 6: "빗방울·눈날림", 7: "눈날림" };
  const SKY = { 1: "맑음", 3: "구름많음", 4: "흐림" };
  const WIND16 = ["북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동", "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서"];
  const RADAR_BASE = "https://www.kma.go.kr/repositary/image/rdr/img/RDR_CMP_WRC_";
  const RADAR_PAGE = "https://www.weather.go.kr/w/image/radar.do";

  /** 그 자료를 확인한 지 몇 분 됐는지. 확인한 적이 없으면 Infinity. */
  function ageMinutes(section) {
    const stamp = section && section.checked_at;
    if (!stamp) return Infinity;
    const t = new Date(stamp).getTime();
    return Number.isFinite(t) ? Math.max(0, (Date.now() - t) / 60000) : Infinity;
  }

  /** 쓸 수 있는 자료인가: 받아 왔고, 기준 시간 안에 확인한 것. */
  const fresh = (section, staleAfterMinutes) => Boolean(section && section.available) && ageMinutes(section) <= staleAfterMinutes;

  async function load(config) {
    if (!config || !config.url) return null;
    const response = await fetch(config.url, { cache: "no-store" });
    if (!response.ok) throw new Error(`실시간 자료: HTTP ${response.status}`);
    const doc = await response.json();
    return doc && doc.schema === 1 ? doc : null;
  }

  const copy = (value) => (typeof structuredClone === "function" ? structuredClone(value) : JSON.parse(JSON.stringify(value)));

  /** latest.json 위에 실시간 특보를 덮는다(원본은 건드리지 않는다). 계산 규칙은 서버(teukbo.assess)와 같다:
   *  발효 중이면 '발효 중', 예비특보가 있으면 최소 '높음'. 실시간 특보를 쓸 수 없으면 원본을 그대로 돌려준다. */
  function applyToLatest(base, live, staleAfterMinutes) {
    if (!base || !live || !fresh(live.warnings, staleAfterMinutes)) return base;
    const latest = copy(base);
    for (const region of latest.regions) {
      const lr = (live.regions || {})[region.id];
      if (!lr || !lr.official) continue;
      region.official = lr.official;
      let top = null;
      for (const hazard of latest.hazards) {
        const sum = region.hazards[hazard.id];
        if (!sum) continue;
        const off = (lr.hz || {})[hazard.id];
        if (hazard.method === "official_only" && lr.official.available) sum.mg = "low";
        delete sum.official; delete sum.official_level; delete sum.official_from;
        if (off) {
          sum.official = off[0];
          sum.official_level = off[1];
          if (off[2]) sum.official_from = off[2];
        }
        const model = sum.mg === "unknown" && off ? "low" : sum.mg;
        sum.g = !off ? model : off[0] === "active" ? "active" : RANK[model] < RANK.high ? "high" : model;
        if (sum.g !== "unknown" && (!top || RANK[sum.g] > RANK[top.grade] || (RANK[sum.g] === RANK[top.grade] && (sum.p || 0) > (top.p || 0)))) {
          top = { hazard: hazard.id, grade: sum.g, p: sum.p };
        }
      }
      region.top = top;
    }
    latest.sources = { ...latest.sources, kma_warnings: { ...(latest.sources || {}).kma_warnings, ok: true, provider: live.warnings.provider } };
    latest.warnings_checked_at = live.warnings.checked_at;
    return latest;
  }

  /** 특보가 바뀌었는지 비교할 때 쓰는 문자열. */
  function warningsSignature(live) {
    if (!live || !live.warnings || !live.warnings.available) return "";
    const key = (w) => `${w.code}|${w.name}|${w.from || ""}|${w.expected || ""}`;
    return [...(live.warnings.active || []).map(key), "/", ...(live.warnings.preliminary || []).map(key)].join(";");
  }

  // ------------------------------------------------------------ 글로 바꾸기

  const windDir = (deg) => (deg == null ? "" : WIND16[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16]);
  const ptyText = (code) => PTY[code] || "";
  /** 하늘·강수형태를 한 낱말로: 비나 눈이 오면 그것, 아니면 하늘상태. */
  const skyText = (row) => ptyText(row.pty) || SKY[row.sky] || "";
  const num = (value, digits = 1) => (value == null ? "–" : Number(value).toFixed(digits).replace(/\.0+$/, ""));

  // ------------------------------------------------------------ 비구름 레이더 (기상청 합성영상, 5분 간격)

  const pad = (n) => String(n).padStart(2, "0");
  /** 한국 시각 기준 5분 단위 시각표(yyyymmddHHMM)와 표시용 글. back = 몇 번째 이전 영상인지. */
  function radarFrame(back) {
    const kst = new Date(Math.floor((Date.now() + 9 * 3600000) / 300000) * 300000 - back * 300000);
    const stamp = `${kst.getUTCFullYear()}${pad(kst.getUTCMonth() + 1)}${pad(kst.getUTCDate())}${pad(kst.getUTCHours())}${pad(kst.getUTCMinutes())}`;
    return { stamp, t: kst.getTime(), url: `${RADAR_BASE}${stamp}.png`, label: `${kst.getUTCMonth() + 1}월 ${kst.getUTCDate()}일 ${pad(kst.getUTCHours())}:${pad(kst.getUTCMinutes())}` };
  }

  const preload = (frame) => new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(frame);
    img.onerror = () => resolve(null);
    img.src = frame.url;
  });

  /** 레이더 영상을 element 안에 그린다. 가장 최근 영상을 찾아 보여 주고 5분마다 새로 찾는다. */
  function mountRadar(element) {
    if (!element || element.dataset.mounted) return;
    element.dataset.mounted = "1";
    const el = (tag, attrs, ...children) => {
      const node = document.createElement(tag);
      for (const [key, value] of Object.entries(attrs || {})) { if (key === "text") node.textContent = value; else node.setAttribute(key, value); }
      node.append(...children);
      return node;
    };
    const img = el("img", { alt: "기상청 레이더 합성영상", width: "635", height: "620", decoding: "async" });
    const status = el("p", { class: "radar-time", "aria-live": "polite", text: "레이더 영상을 찾는 중…" });
    const play = el("button", { type: "button", class: "chip", "aria-pressed": "false", text: "최근 1시간 재생" });
    const foot = el("p", { class: "official-foot" }, "기상청 레이더 합성영상(5분 간격, 강수 강도 mm/h) · ", el("a", { href: RADAR_PAGE, rel: "noopener", target: "_blank", text: "날씨누리에서 크게 보기" }));
    const figure = el("div", { class: "radar-img" }, img);
    element.append(el("div", { class: "radar-head" }, status, play), figure, foot);

    let latest = null, timer = 0, frames = [];
    const show = (frame, note) => {
      img.src = frame.url;
      img.alt = `기상청 레이더 합성영상 ${frame.label}`;
      status.textContent = `${frame.label} 관측${note || ""}`;
    };
    const stop = () => { clearInterval(timer); timer = 0; play.setAttribute("aria-pressed", "false"); play.textContent = "최근 1시간 재생"; if (latest) show(latest); };

    async function refresh() {
      if (timer) return;  // 재생 중에는 바꾸지 않는다
      // 영상은 관측 뒤 몇 분 지나 올라온다 → 가장 최근 시각부터 거슬러 올라가며 찾는다
      for (let back = 0; back <= 6; back++) {
        const found = await preload(radarFrame(back));
        if (found) {
          if (!latest || latest.stamp !== found.stamp) { latest = found; frames = []; show(found); }
          figure.hidden = false;
          return;
        }
      }
      if (!latest) { status.textContent = "레이더 영상을 불러오지 못했습니다. 날씨누리에서 확인하세요."; figure.hidden = true; }
    }

    play.addEventListener("click", async () => {
      if (timer) { stop(); return; }
      if (!latest) return;
      play.disabled = true;
      play.textContent = "불러오는 중…";
      if (!frames.length) {
        const base = Math.floor((Date.now() + 9 * 3600000) / 300000) - latest.t / 300000;  // 가장 최근 영상이 지금부터 몇 번째 이전인지
        const wanted = [];
        for (let k = 12; k >= 0; k--) wanted.push(radarFrame(base + k));
        frames = (await Promise.all(wanted.map(preload))).filter(Boolean);
      }
      play.disabled = false;
      if (frames.length < 2) { play.textContent = "최근 1시간 재생"; return; }
      let i = 0;
      play.setAttribute("aria-pressed", "true");
      play.textContent = "멈춤";
      show(frames[0], " (재생 중)");
      timer = setInterval(() => { i = (i + 1) % frames.length; show(frames[i], i === frames.length - 1 ? " (가장 최근)" : " (재생 중)"); }, 600);
    });

    refresh();
    setInterval(() => { if (!document.hidden) refresh(); }, 5 * 60 * 1000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  }

  return { ageMinutes, fresh, load, applyToLatest, warningsSignature, windDir, ptyText, skyText, num, mountRadar };
})();
