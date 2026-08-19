/* ComfyUI 遥控 Service Worker
 * 策略：app shell（index.html / css / js）网络优先（局域网控制层几乎总在线，
 * 刷新即拿最新前端，离线时才回退缓存兜底）；/api/* 与 /api/images/* 纯网络，
 * 绝不回退到过期缓存（控制面板的 API 响应是瞬态状态，缓存兜底会误导用户）。
 */
'use strict';

const CACHE = 'comfyui-remote-v22';
const SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './css/style.css',
  './js/ui.js',
  './js/ws.js',
  './js/app.js',
  './js/pages/generate.js',
  './js/pages/gallery.js',
  './js/pages/settings.js'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function networkOnly(req) {
  try {
    return await fetch(req);
  } catch (err) {
    return new Response(JSON.stringify({ error: 'network_error' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' }
    });
  }
}

// 网络优先：在线拿最新（并回填缓存），离线回退缓存；导航离线回退 index.html
async function networkFirst(req) {
  try {
    const res = await fetch(req);
    if (res && res.ok) {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
    }
    return res;
  } catch (err) {
    const cached = await caches.match(req);
    if (cached) return cached;
    if (req.mode === 'navigate') {
      const idx = await caches.match('./index.html');
      if (idx) return idx;
    }
    return new Response('', { status: 408 });
  }
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return; // POST/PUT/DELETE 直接透传
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // 跨源（如 cdn）不拦截
  if (url.pathname.startsWith('/preview')) return; // 设计预览页不拦截不缓存，迭代即时生效
  const isApi = url.pathname.startsWith('/api/');
  e.respondWith(isApi ? networkOnly(req) : networkFirst(req));
});
