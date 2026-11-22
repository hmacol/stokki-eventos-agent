// Service worker do PWA do motorista.
// Cuida só do app (arquivos do próprio domínio). Chamadas da API (/api/...)
// passam direto: nunca ficam em cache, senão o motorista veria rota velha
// como se fosse atual.
//  - Navegação (abrir o app): rede primeiro; sem sinal, usa a última cópia.
//  - Arquivos do app (_expo/static/... têm hash no nome): cache primeiro.
const CACHE = 'freshlog-motorista-v1';
const BASE = '/';
const ehApp = (url) => url.origin === self.location.origin && !url.pathname.startsWith('/api/');

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.add(BASE)).catch(() => undefined));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((nomes) => Promise.all(nomes.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim()),
  );
});

// A página manda o que já carregou na 1ª visita (antes do SW assumir).
self.addEventListener('message', (e) => {
  const d = e.data;
  if (!d || d.tipo !== 'guardar' || !Array.isArray(d.urls)) return;
  e.waitUntil(caches.open(CACHE).then((c) => Promise.all(d.urls.map((u) => {
    try {
      const url = new URL(u);
      if (!ehApp(url)) return undefined;
      return c.match(u).then((ja) => ja || c.add(u)).catch(() => undefined);
    } catch {
      return undefined;
    }
  }))));
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (!ehApp(url)) return;

  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req)
        .then((r) => {
          if (r.ok) { const copia = r.clone(); caches.open(CACHE).then((c) => c.put(BASE, copia)); }
          return r;
        })
        .catch(() => caches.match(BASE).then((r) => r || Response.error())),
    );
    return;
  }

  e.respondWith(
    caches.match(req).then((guardado) => guardado || fetch(req).then((r) => {
      if (r.ok) { const copia = r.clone(); caches.open(CACHE).then((c) => c.put(req, copia)); }
      return r;
    })),
  );
});
