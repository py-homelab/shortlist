// The smallest service worker that makes the app installable: it caches nothing and answers every
// fetch from the network. Offline picks would be stale picks — the list is rebuilt nightly, and an
// action taken offline could act on a title no longer offered. Installability is the whole point:
// the picks page on a phone's home screen, full-screen, with the deck owning the viewport.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
