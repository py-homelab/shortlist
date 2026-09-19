---
title: Exposing Shortlist to the internet
description: "What to know before publishing Shortlist outside your network: TLS, proxies, the API token, and what's in a backup."
heading: Putting it on the internet
nav_order: 9
---

## Exposing Shortlist to the internet

Whether Shortlist is reachable from outside your network is **your call**. It is a normal web app and
it does not assume either way. If you do publish it, these are the things worth knowing.

**Put it behind a reverse proxy with HTTPS.** The session cookie is only marked `Secure` when the
request arrives over TLS, and HSTS is only sent then too. Over plain HTTP neither protects anything.

**`FORWARDED_ALLOW_IPS` defaults to `*`**, which trusts the `X-Forwarded-For` of whatever connects.
That is convenient when your proxy is on another host, but it means the client IP Shortlist logs is whatever
the caller claims. Set it to your proxy's address if you publish the container port directly.

**Only `/api/system/health` answers without a login**, and it returns nothing but `{"status": "ok"}`.
Everything else requires a Plex sign-in, re-checked on every request. The owner's account reaches
Shortlist; any other person on the server reaches **their own picks page** (`/me`) and nothing
else — see [Everyone's own picks](picks.md#everyones-own-picks). Login is rate-limited, and so are
failed API-token attempts.

**Signing people in through your own proxy.** If a reverse proxy in front of Shortlist already
authenticates visitors (authentik, Authelia, oauth2-proxy), it can name them to Shortlist instead
of each person signing in with Plex twice: **Settings → Advanced → Trusted proxy sign-in**. The proxy
sends the visitor's Plex account id in the header you name there, and proves it is the proxy with a
shared secret in `X-Shortlist-Proxy-Secret`. The secret is what makes the header trustworthy — with
`FORWARDED_ALLOW_IPS` at `*`, a client address alone proves nothing, and anyone could send the id
header. A request without the secret is simply anonymous, never refused, so a misconfigured proxy
degrades to the login screen. Keep the admin pages for yourself with the proxy's own rules
(everything but `/me`, `/api/me/*` and `/api/auth/*`), or rely on Shortlist's own check — a person
named by the proxy is still only a person here.

**The API token is owner-level access.** Anything holding it can do anything you can, including
deleting rows and rewriting share filters. Rotate it from Settings → API access if it leaks; the old
one stops working immediately.

**URLs you enter are fetched by the server**, which is the point: your Plex, Tautulli, Radarr,
Sonarr and Ollama are usually on private addresses, and all of those keep working. The only thing
Shortlist refuses is a cloud instance-metadata address (`169.254.169.254` and friends), which no media
server runs on and which hands out credentials on a hosted VM.

**Backups contain everything**, including your Plex token and the pre-Shortlist share-filter snapshots
that Uninstall restores from. They sit in `/config/backups`. Treat that directory like a password
file.
