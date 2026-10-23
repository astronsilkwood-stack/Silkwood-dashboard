// Silkwood service worker: caches the app shell only. API/data always go to the network.
const V='silkwood-shell-v1',SHELL=['/','/static/manifest.webmanifest','/static/icon-192.png','/static/icon-512.png','/static/apple-touch-icon.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(V).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==V).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{const r=e.request,u=new URL(r.url);
 if(r.method!=='GET'||u.origin!==location.origin)return;               // never touch POST/DELETE or other sites
 if(u.pathname.startsWith('/api/')||u.pathname.startsWith('/submission/')||u.pathname==='/healthz')return; // data: network only, never cached
 if(r.mode==='navigate'||u.pathname==='/'||u.pathname==='/index.html'){   // shell: network-first, cache fallback offline
  e.respondWith(fetch(r).then(res=>{if(res.ok){const cp=res.clone();caches.open(V).then(c=>c.put('/',cp))}return res}).catch(()=>caches.match('/')));return}
 if(u.pathname.startsWith('/static/'))e.respondWith(caches.match(r).then(m=>m||fetch(r)));
});
