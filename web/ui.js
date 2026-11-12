/* 화면 공용 도우미 — DOM 만들기, 한국 시각 표기, 숫자 표기. (분석 화면이 쓴다) */
window.TeukboUI = (() => {
  "use strict";
  const SVG_NS = "http://www.w3.org/2000/svg";

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
  /** HTML 요소. 글은 늘 텍스트로 넣는다(받아 온 자료를 HTML 로 해석하지 않는다). */
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
  const fmtDate = (ymd) => { const [, m, d] = ymd.split("-"); return `${Number(m)}월 ${Number(d)}일`; };
  function fmtAgo(iso) {
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 1) return "방금";
    if (minutes < 60) return `${minutes}분 전`;
    const hours = Math.floor(minutes / 60);
    return hours < 48 ? `${hours}시간 ${minutes % 60 ? `${minutes % 60}분 ` : ""}전` : `${Math.floor(hours / 24)}일 전`;
  }
  const num = (value, digits = 1) => (value == null ? "–" : Number(value).toFixed(digits).replace(/\.0+$/, ""));
  /** 부호를 붙인 숫자: +3.2, −0.4, 0 */
  const signed = (value, digits = 1) => (value == null ? "–" : `${value > 0 ? "+" : value < 0 ? "−" : ""}${num(Math.abs(value), digits)}`);
  const share = (part, total) => {
    if (!total) return "–";
    const pct = (part / total) * 100;
    return part > 0 && pct < 1 ? "1% 미만" : `${Math.round(pct)}%`;
  };
  const stationLabel = (name) => name.replace(/^S(?=[가-힣])/, "");

  async function getJson(path) {
    const response = await fetch(path, { cache: "no-cache" });
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    return response.json();
  }

  return { h, s, $, clear, fmtClock, fmtDate, fmtAgo, num, signed, share, stationLabel, getJson };
})();
