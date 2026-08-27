/* The Associate — 서비스워커.
 *
 * 이 앱은 **로그인 뒤에 사용자별 데이터를 보여주는 앱**이다. 그래서 "오프라인에서도 다 되게"
 * 캐싱하면 안 된다. 남의 기기·다른 계정에 남은 응답을 다시 보여줄 위험이 실제로 생긴다.
 *
 * 그래서 역할을 최소로 자른다:
 *   1) 설치 가능하게 만든다 (Chrome 은 fetch 핸들러가 있는 SW 를 요구한다)
 *   2) 정적 자산(css/js/아이콘)만 stale-while-revalidate 로 빠르게 띄운다
 *   3) API·HTML·엑셀 다운로드는 **절대 캐시하지 않는다** (network-only)
 *   4) 네트워크가 끊긴 화면 진입에만 안내 페이지를 준다
 *
 * 캐시 이름에 버전을 박아 배포할 때마다 통째로 교체한다.
 */
const VERSION = 'assoc-v1';
const STATIC_CACHE = `${VERSION}-static`;

/* 앱 껍데기. 여기에 HTML 을 넣지 않는 것이 중요하다 — index.html 은 인증 상태에 따라
 * 달라질 수 있고, 캐시된 껍데기를 보여주면 로그아웃 상태가 로그인처럼 보인다. */
const PRECACHE = [
  '/static/styles.css',
  '/static/app.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      // 하나가 404 여도 설치 자체가 실패하지 않도록 개별로 담는다.
      .then((cache) => Promise.allSettled(PRECACHE.map((u) => cache.add(u))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== STATIC_CACHE).map((k) => caches.delete(k)),
      ))
      .then(() => self.clients.claim()),
  );
});

/* 새 버전이 대기 중일 때 페이지가 즉시 교체를 요청할 수 있게 한다. */
self.addEventListener('message', (event) => {
  if (event.data === 'skip-waiting') self.skipWaiting();
});

const OFFLINE_HTML = `<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>오프라인</title>
<style>
  html,body{height:100%;margin:0;background:#0A0E17;color:#E6EAF2;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    display:flex;align-items:center;justify-content:center;text-align:center}
  .box{padding:2rem;max-width:22rem}
  .mark{color:#F0B429;font-weight:600;font-size:1.4rem;letter-spacing:.3em}
  p{color:#9AA6B8;line-height:1.6;margin:1.2rem 0 0}
  button{margin-top:1.4rem;padding:.6rem 1.1rem;border-radius:8px;
    border:1px solid #F0B429;background:transparent;color:#F0B429;font-size:1rem}
</style></head><body><div class="box">
<div class="mark">THE ASSOCIATE</div>
<p>네트워크에 연결되지 않았습니다.<br>이 앱은 공시 API 를 실시간으로 조회하므로
오프라인에서는 조회할 수 없습니다.</p>
<button onclick="location.reload()">다시 시도</button>
</div></body></html>`;

function isStaticAsset(url) {
  return url.origin === self.location.origin
    && (url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest');
}

self.addEventListener('fetch', (event) => {
  const { request } = event;

  /* GET 이 아닌 것(질문 전송, 엑셀 내보내기 등)은 손대지 않는다. */
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  /* 외부 도메인은 통과 */
  if (url.origin !== self.location.origin) return;

  /* API·인증·다운로드는 절대 캐시하지 않는다 — 오래된 재무수치를 보여주는 것이
   * 이 앱에서는 곧 오답이다. */
  if (url.pathname.startsWith('/api/') || url.pathname === '/healthz') return;

  /* 정적 자산: 캐시 우선 + 뒤에서 갱신(stale-while-revalidate) */
  if (isStaticAsset(url)) {
    event.respondWith((async () => {
      const cache = await caches.open(STATIC_CACHE);
      const hit = await cache.match(request);
      const fetching = fetch(request)
        .then((res) => {
          if (res && res.ok) cache.put(request, res.clone());
          return res;
        })
        .catch(() => null);
      return hit || (await fetching) || new Response('', { status: 504 });
    })());
    return;
  }

  /* 화면 진입(navigation): 항상 네트워크. 실패했을 때만 오프라인 안내. */
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => new Response(OFFLINE_HTML, {
        status: 200,
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
      })),
    );
  }
});
