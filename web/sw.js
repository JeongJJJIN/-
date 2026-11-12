/* 서비스 워커 — 오프라인에서도 마지막으로 본 화면과 자료를 보여 준다.
 * 전략: 네트워크 우선(최신 자료가 항상 먼저), 실패하거나 느리면 저장해 둔 사본.
 * 캐시 이름의 빌드 번호(__BUILD__)는 배포 때 바뀌어, 새 버전이 깔리면 옛 캐시를 지운다.
 */
const CACHE = "teukbo-__BUILD__";
const SHELL = ["./", "index.html", "home.js?v=__BUILD__", "full.html", "style.css?v=__BUILD__", "live.js?v=__BUILD__", "app.js?v=__BUILD__", "rail.html", "rail.js?v=__BUILD__", "analysis.html", "ui.js?v=__BUILD__", "analysis-core.js?v=__BUILD__", "analysis.js?v=__BUILD__",
  "geo/zones.json", "geo/rail.json", "geo/stations.json", "geo/lines.json", "manifest.webmanifest", "icons/icon.svg", "icons/icon-192.png"];
const NETWORK_TIMEOUT_MS = 6000;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).catch(() => undefined).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key.startsWith("teukbo-") && key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

function fetchWithTimeout(request) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), NETWORK_TIMEOUT_MS);
    fetch(request).then(
      (response) => { clearTimeout(timer); resolve(response); },
      (error) => { clearTimeout(timer); reject(error); }
    );
  });
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" || new URL(request.url).origin !== self.location.origin) return;
  event.respondWith(
    fetchWithTimeout(request)
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => undefined);
        }
        return response;
      })
      .catch(() =>
        caches.match(request, { ignoreSearch: request.mode === "navigate" }).then((cached) => {
          if (cached) return cached;
          if (request.mode === "navigate") return caches.match("index.html");
          return Response.error();
        })
      )
  );
});
