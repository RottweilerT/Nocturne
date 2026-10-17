// Mode hors ligne : la page et ses ressources sont gardées sur l'appareil.
// La page est redemandée au réseau d'abord (pour avoir la dernière version) ;
// sans réponse rapide ou sans connexion, la copie locale prend le relais.
// Les données Firebase ne passent pas par ici.
const CACHE = 'nocturne-v9';
const CORE = ['./', './index.html', './manifest.webmanifest', './icon-192.png', './icon-512.png', './firebase-config.js', './night.jpg', './sunset.jpg', './standing.jpg', './apple-touch-icon.png'];
const EXTERNAL = ['fonts.googleapis.com', 'fonts.gstatic.com'];
const PAGE = new URL('./index.html', self.registration.scope).href;
const NET_TIMEOUT = 4000;   // réseau trop lent (réception faible la nuit) : on sert la copie locale

// Une réponse issue d'une redirection ne peut pas servir une page : on en fait une copie « propre »
async function clean(res) {
  if (!res || !res.redirected) return res;
  const body = await res.blob();
  return new Response(body, { status: res.status, statusText: res.statusText, headers: res.headers });
}

self.addEventListener('install', e => {
  // Chaque fichier est mis en cache séparément : un échec isolé ne bloque pas l'installation
  e.waitUntil(caches.open(CACHE).then(c => Promise.all(CORE.map(async u => {
    try { const r = await fetch(u, { cache: 'reload' }); if (r.ok) await c.put(u, await clean(r)); } catch (err) {}
  }))).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k.startsWith('nocturne-') && k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function cachedPage() {
  const r = (await caches.match(PAGE)) || (await caches.match(new URL('./', self.registration.scope).href));
  if (r) return clean(r);
  return new Response(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nocturne</title><body style="margin:0;background:#0b0d17;color:#ebe5d8;font:16px system-ui;display:grid;place-items:center;min-height:100vh;text-align:center;padding:24px">
<div><h1 style="font-weight:400">Nocturne n'est pas encore disponible hors ligne</h1>
<p>Ouvre l'app une fois avec Internet (Wi-Fi ou 4G), attends quelques secondes, puis elle fonctionnera sans réseau.</p></div>`,
    { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      const net = fetch(req).then(async res => {
        if (res.ok && url.origin === location.origin) {
          const copy = await clean(res.clone());
          caches.open(CACHE).then(c => c.put(PAGE, copy));
        }
        return res;
      });
      net.catch(() => {});
      const timeout = new Promise(r => setTimeout(() => r(null), NET_TIMEOUT));
      try {
        const res = await Promise.race([net, timeout]);
        if (res) return res;
        const local = await caches.match(PAGE);
        return local ? clean(local) : await net;
      } catch (err) {
        return cachedPage();
      }
    })());
    return;
  }

  const static_ = url.origin === location.origin || EXTERNAL.includes(url.hostname)
    || (url.hostname === 'www.gstatic.com' && url.pathname.startsWith('/firebasejs/'));
  if (!static_) return;
  e.respondWith(caches.open(CACHE).then(async c => {
    const cached = await c.match(req, { ignoreSearch: url.origin === location.origin });
    const fresh = fetch(req).then(res => {
      if (res.ok || res.type === 'opaque') c.put(req, res.clone());
      return res;
    }).catch(() => cached || Response.error());
    return cached || fresh;
  }));
});
