/* FinVault — service worker : installation + ouverture hors-ligne.
   Les cours (prices.json sur raw.githubusercontent.com) et les API ne sont
   JAMAIS interceptes : les donnees restent toujours fraiches. */
const CACHE = 'finvault-v1';
const SHELL = ['./', 'index.html', 'manifest.json', 'icon-192.png', 'icon-512.png'];
const LIBS  = ['cdnjs.cloudflare.com', 'fonts.googleapis.com', 'fonts.gstatic.com'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c =>
    Promise.all(SHELL.map(u => c.add(u).catch(() => {})))).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Page principale : reseau d'abord (nouvelle version des qu'elle est en ligne), cache en secours
  if (url.origin === location.origin && (req.mode === 'navigate' || url.pathname.endsWith('/index.html'))) {
    e.respondWith(fetch(req).then(r => {
      const copy = r.clone(); caches.open(CACHE).then(c => c.put('index.html', copy)); return r;
    }).catch(() => caches.match('index.html')));
    return;
  }
  // Icones, manifeste : cache d'abord
  if (url.origin === location.origin) {
    e.respondWith(caches.match(req).then(h => h || fetch(req)));
    return;
  }
  // Bibliotheques et polices : cache puis mise a jour en arriere-plan
  if (LIBS.includes(url.hostname)) {
    e.respondWith(caches.open(CACHE).then(c => c.match(req).then(h => {
      const net = fetch(req).then(r => { c.put(req, r.clone()); return r; }).catch(() => h);
      return h || net;
    })));
  }
  // Tout le reste (cours, Yahoo, etc.) : non intercepte
});
