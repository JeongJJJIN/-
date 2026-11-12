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
    return { stamp, t: kst.getTime(), url: `${RADAR_BASE}${stamp}.png`, clock: `${pad(kst.getUTCHours())}:${pad(kst.getUTCMinutes())}`,
      label: `${kst.getUTCMonth() + 1}월 ${kst.getUTCDate()}일 ${pad(kst.getUTCHours())}:${pad(kst.getUTCMinutes())}` };
  }

  const preload = (frame) => new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(frame);
    img.onerror = () => resolve(null);
    img.src = frame.url;
  });

  /** 레이더 영상 찾기·재생을 맡는다. 그리는 일은 부르는 쪽이 한다.
   *  handlers.show(frame, note)  — 이 영상을 보여 달라 (note: "", " (재생 중)", " (가장 최근)")
   *  handlers.missing()          — 영상을 하나도 찾지 못했다
   *  handlers.playing(state)     — "idle" | "loading" | "playing" */
  function createRadar(handlers) {
    let latest = null, timer = 0, frames = [];
    const state = (value) => { if (handlers.playing) handlers.playing(value); };

    async function refresh() {
      if (timer) return;  // 재생 중에는 바꾸지 않는다
      // 영상은 관측 뒤 몇 분 지나 올라온다 → 가장 최근 시각부터 거슬러 올라가며 찾는다
      for (let back = 0; back <= 6; back++) {
        const found = await preload(radarFrame(back));
        if (found) {
          if (!latest || latest.stamp !== found.stamp) { latest = found; frames = []; }
          handlers.show(latest, "");
          return;
        }
      }
      if (!latest) handlers.missing();
    }

    function stop() {
      if (!timer) return;
      clearInterval(timer);
      timer = 0;
      state("idle");
      if (latest) handlers.show(latest, "");
    }

    async function togglePlay() {
      if (timer) { stop(); return; }
      if (!latest) return;
      state("loading");
      if (!frames.length) {
        const base = Math.floor((Date.now() + 9 * 3600000) / 300000) - latest.t / 300000;  // 가장 최근 영상이 지금부터 몇 번째 이전인지
        const wanted = [];
        for (let k = 12; k >= 0; k--) wanted.push(radarFrame(base + k));
        frames = (await Promise.all(wanted.map(preload))).filter(Boolean);
      }
      if (frames.length < 2) { state("idle"); return; }
      let i = 0;
      state("playing");
      handlers.show(frames[0], " (재생 중)");
      timer = setInterval(() => { i = (i + 1) % frames.length; handlers.show(frames[i], i === frames.length - 1 ? " (가장 최근)" : " (재생 중)"); }, 600);
    }

    setInterval(() => { if (!document.hidden) refresh(); }, 5 * 60 * 1000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    return { refresh, togglePlay, stop };
  }

  /** 레이더 영상을 element 안에 그림 그대로 보여 준다(첫 화면용). */
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
    const foot = el("p", { class: "official-foot" }, "기상청 레이더 합성영상(5분 간격, 강수 강도 mm/h) · ", el("a", { href: "rail.html", text: "지도에서 특보·철도와 함께 보기" }), " · ",
      el("a", { href: RADAR_PAGE, rel: "noopener", target: "_blank", text: "날씨누리" }));
    const figure = el("div", { class: "radar-img" }, img);
    element.append(el("div", { class: "radar-head" }, status, play), figure, foot);
    const radar = createRadar({
      show(frame, note) { img.src = frame.url; img.alt = `기상청 레이더 합성영상 ${frame.label}`; status.textContent = `${frame.label} 관측${note}`; figure.hidden = false; },
      missing() { status.textContent = "레이더 영상을 불러오지 못했습니다. 날씨누리에서 확인하세요."; figure.hidden = true; },
      playing(state) { playButton(play, state); },
    });
    play.addEventListener("click", () => radar.togglePlay());
    radar.refresh();
  }

  /** 재생 단추의 글과 상태를 맞춘다. */
  function playButton(button, state) {
    button.disabled = state === "loading";
    button.setAttribute("aria-pressed", String(state === "playing"));
    button.textContent = state === "loading" ? "불러오는 중…" : state === "playing" ? "멈춤" : "최근 1시간 재생";
  }

  // 레이더 합성영상(635×620)의 지도 좌표: 람베르트 정각원추도법(표준위도 30°·60°, 기준점 126°E 38°N, 지구 반지름 6371.00877km)
  // — 기상청 동네예보 격자와 같은 도법이다. 축척과 기준점의 화소 위치는 영상의 해안선·시도 경계에 맞춰 구했다(tools/radar_calibrate.py).
  const RADAR_IMAGE = { kmPerPx: 1.708, x0: 258.0, y0: 169.75, width: 635, height: 620, frame: { x: 1, y: 21, w: 598, h: 598 } };
  const lcc = (() => {
    const d = Math.PI / 180, RE = 6371.00877, s1 = 30 * d, s2 = 60 * d, olon = 126 * d, olat = 38 * d;
    const sn = Math.log(Math.cos(s1) / Math.cos(s2)) / Math.log(Math.tan(Math.PI / 4 + s2 / 2) / Math.tan(Math.PI / 4 + s1 / 2));
    const sf = (Math.tan(Math.PI / 4 + s1 / 2) ** sn * Math.cos(s1)) / sn;
    const ro = (RE * sf) / Math.tan(Math.PI / 4 + olat / 2) ** sn;
    return (lon, lat) => {
      const ra = (RE * sf) / Math.tan(Math.PI / 4 + (lat * d) / 2) ** sn, theta = (lon * d - olon) * sn;
      return [ra * Math.sin(theta), ro - ra * Math.cos(theta)];  // 기준점에서 동쪽·북쪽으로 몇 km
    };
  })();
  /** 경위도 → 레이더 영상의 화소 좌표 [x, y] (철도 지도의 지도 좌표로도 쓴다). */
  const radarProject = (lon, lat) => { const [east, north] = lcc(lon, lat); return [RADAR_IMAGE.x0 + east / RADAR_IMAGE.kmPerPx, RADAR_IMAGE.y0 - north / RADAR_IMAGE.kmPerPx]; };

  /** 레이더 강수 강도 색(기상청 영상의 범례 그대로, 약한 비 → 강한 비). [아래 경계 mm/h, 색] */
  const RADAR_SCALE = [
    [0, "#00c8ff"], [0.1, "#009bf5"], [0.5, "#004af5"], [1, "#00ff00"], [2, "#00be00"], [3, "#008c00"], [4, "#005a00"],
    [5, "#ffff00"], [6, "#ffdc1f"], [7, "#f9cd00"], [8, "#e0b900"], [9, "#ccaa00"], [10, "#ff6600"], [15, "#ff3200"], [20, "#d20000"], [25, "#b40000"],
    [30, "#e0a9ff"], [40, "#c969ff"], [50, "#b329ff"], [60, "#9300e4"], [70, "#b3b4de"], [90, "#4c4eb1"], [110, "#000390"], [150, "#333333"],
  ];

  return { ageMinutes, fresh, load, applyToLatest, warningsSignature, windDir, ptyText, skyText, num, mountRadar, createRadar, playButton, RADAR_SCALE, RADAR_IMAGE, radarProject };
})();
