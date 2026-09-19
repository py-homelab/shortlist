---
title: Requests
description: How people ask for titles the library doesn't have — each from their own picks page, as themselves, through Overseerr, Jellyseerr or Seerr.
heading: Requests
nav_order: 7
---

## Requests are each person's own

Every person on the server has their own picks page (`/me`): the titles their watch history points
at that the library doesn't hold yet, ranked for them. When they swipe right on one, Shortlist files
the request in your Overseerr, Jellyseerr or Seerr **as them** — their account, their quota, their
approval rules. A request from someone whose account needs approval waits for it there, exactly as if
they had asked in the app themselves. Nothing is ever requested without a person asking.

How the page works — the deck, the grid, never / later / skip, the family lane — is in
[Everyone's own picks](picks.md#everyones-own-picks).

## Setting it up

**Settings → Connections → Overseerr / Jellyseerr / Seerr**: the address and an API key.

- The key needs the **Manage Users** permission. Shortlist matches each person's Plex account to their
  account in the request app through it; without it nobody can be matched and requests stay off.
- Each person needs an account in the request app linked to the same Plex account — the one they get
  by signing in to it with Plex once. Someone without one sees their picks with requests switched
  off, and a line saying why.
- A show asks for its **first regular season**. More seasons are added in the request app.

Settings keys: `seerr.url`, `seerr.apikey` (encrypted).

## What changed

Shortlist used to keep an owner-only **approval inbox**: titles many people's rows pointed at,
sorted by how many wanted them, sent to Radarr, Sonarr or Overseerr automatically or on your
approval. That inbox, its Radarr/Sonarr routing, the request gates and tags, and the per-row request
settings are gone. They answered "what does the household want most", which surfaced the same
well-known titles for everyone; the per-person page answers "what does this person want", and lets
them say so. The `requests.overseerr.url` / `requests.overseerr.apikey` settings were moved to
`seerr.url` / `seerr.apikey` on upgrade, and `requests.mdblist.apikey` to
`recommendations.mdblist.apikey` (still used for "Highest rated" row ordering).
