# Cinemark D-BOX Tracker

Tracks **realized D-BOX sell-through** across Cinemark's US chain — how full each
D-BOX (premium motion-seat) showing actually got by the time it played — using the
same realized-metric model as the Cineplex tracker, but built fresh for Cinemark.

This is a **standalone project** with its own git repo. It does not touch the
Cineplex tracker or its dashboard.

## How Cinemark exposes D-BOX (the key)

Cinemark has no JSON API like Cineplex — it server-renders HTML. A D-BOX screening
is sold as a **pair of linked showtimes in one auditorium**:

- regular seats under the **primary** `ShowtimeId` (`seatType="seat"`)
- D-BOX seats under the **linked** `ShowtimeId` (`seatType="dbox"`)

The seat map only includes the D-BOX seats when the request carries
`&LinkedShowtimeId=<linked>`. So:

- **Discovery** — a showtime is D-BOX iff its `/TicketSeatMap/` link has a
  `LinkedShowtimeId`.
- **Measurement** — fetch the seat map *with* the `LinkedShowtimeId`, split
  `seatType="dbox"` (D-BOX) vs the rest of the house, and count `available="False"`
  as sold. This gives D-BOX sell-through plus a D-BOX-vs-rest-of-house comparison,
  exactly like the Cineplex tool.

## Pipeline

```
/sitemap.xml               -> every OPEN US theatre (~300; the /d-box-theatres
                              page is location-filtered and only shows ~38, so we
                              don't use it). Closed/coming-soon are skipped.
/theatres/<slug>           -> that theatre's showtimes (HTML); keep linked = D-BOX
/TicketSeatMap/?...&LinkedShowtimeId=...
                           -> every seat as a <button>; count D-BOX sold
```

Discovery scans the whole chain on a **full pass** (all ~300 theatres), records
which ones actually have D-BOX into `dbox_theatres_cache.json`, then for the next
week only re-scans those cached theatres (fast + light). It does a fresh full pass
weekly to catch new D-BOX rollouts; force one with `discover --full`.

## Commands

```bash
# Offline sanity check on bundled fixtures (no network):
python3 cinemark_scraper.py demo

# Live (needs a US residential IP — Cinemark blocks datacenter IPs):
export CINEMARK_PROXY="http://USER__cr.us:PASS@gw.dataimpulse.com:823"
python3 cinemark_scraper.py discover --date "$(date +%-m/%-d/%Y)"
python3 cinemark_scraper.py measure  --date "$(date +%-m/%-d/%Y)"
```

`discover` writes `schedule_cinemark/<date>.json`; `measure` reads seat maps for
showings near their start time and accumulates realized numbers into
`dashboard/cinemark_data.json`.

## Live access notes

- **Residential IP required.** Cinemark blanks/blocks datacenter IPs. Set
  `CINEMARK_PROXY` to a US residential proxy (the existing DataImpulse account works
  — append `__cr.us` to the username for US exits).
- **Session needed for seat maps.** The seat-map endpoint returns an empty body to a
  cold, cookieless client. The scraper warms a session automatically (cookie jar +
  a homepage hit). For a stubborn local run, paste a browser `Copy as cURL` into
  `cinemark_cookie.txt`.

## Brazil (and worldwide)

Cinemark's international sites are separate platforms, so each country is its own
scraper. **Brazil** (`cinemark_br_scraper.py`) is built: it uses Brazil's JSON BFF
API (`br-www-frontend-ext-prod.cinemark.com.br/bff-api/v1`) — roster from
states→cities→`theaters` (keep `sessionTypes` ⊇ DBOX), discovery from
`sessions/movie` (D-BOX rooms carry feature code 6), and measurement from
`seatmaps` where **seat `type` 12 = D-Box** and `status` 3 = sold. Same realized
metric, same two-read model, writes `dashboard/cinemark_br_data.json`.

**Colombia** (`cinemark_co_scraper.py`) is built too, but on a different stack:
Cinemark's Latin-America "cinemark-core" gateway (`api.cinemark-core.com`), which
fronts a Vista `WSVistaWebClient` OData service. The only auth is a **static,
non-secret header** `connectapitoken: web-co-token` (per-country label) plus an
Origin/Referer — no login, no rotating token. Roster from `/theaters?$format=json`
(OData Cinemas), discovery from `/city/<slug>/movie/<id>` (a session is D-BOX iff its
`Format.SeatTypes` contains `DBOX`), and measurement from
`/cinemas/<id>/sessions/<id>/seat-plan` where the **D-BOX `Area`** (its
`AreaCategory` Name/Description contains `DBOX`) is split from the rest of the house
and seat **`Status` 1 = sold, 0 = available** (broken/space excluded). Same realized
metric, same two-read model, writes `dashboard/cinemark_co_data.json`.

The dashboard has a **United States / Brasil / Colombia toggle** at the top that
switches between the data files. The rest of Cinemark's Latin-America footprint splits
across platforms (verified June 2026): **Argentina / Chile / Peru** share one
ex-Cinemark-Hoyts server-rendered stack (puntospoint + Azure) — a future
`cinemark_southcone_scraper.py` would cover all three; **Ecuador**'s domain has lapsed
to a spam site and is excluded. Each new market = another scraper + data file + toggle
entry.

## Dashboard

`dashboard/index.html` shows the realized D-BOX sell-through headline,
D-BOX-vs-rest-of-house comparison + trend, an upcoming rail, and a sortable table of
completed showings. Serve the `dashboard/` folder via GitHub Pages (see
PROXY_AND_ACTIONS_SETUP.md).

**Combined view (`combined-dashboard` branch).** The page pulls **four data files
live from their raw CDNs and merges whichever the top selector has active**:
Cineplex (`dbox-dashboard` repo's `data.json`) plus Cinemark US / Brasil / Colombia
(this repo). The selector is **All · Cineplex · Cinemark ▾** where the Cinemark
dropdown has per-country checkboxes (United States / Brasil / Colombia). It never
touches any scraper or loop — each pipeline keeps publishing its own file
independently; the dashboard is purely a read-and-merge layer. Rows are tagged
CPX/US/BR/CO when more than one chain is in view. Merging Cineplex's `province`/
`cineplexUrl` shape with Cinemark's `state`/`cinemarkUrl` is handled in `enrich()`.

## Automation

**One self-chaining loop per country**, so logs stay isolated, a problem in one
market can't stall another, and each wakes only when its own showings need a read:

- `.github/workflows/live-loop.yml` — **US**: discover-if-stale + measure, commits
  `cinemark_data.json` + `schedule_cinemark/`. Secrets: `CINEMARK_PROXY` + `DISPATCH_PAT`.
- `.github/workflows/live-loop-br.yml` — **Brazil**: same pattern, commits
  `cinemark_br_data.json` + `schedule_cinemark_br/`. Secret: `DISPATCH_PAT` (shared);
  `CINEMARK_BR_PROXY` optional.
- `.github/workflows/live-loop-co.yml` — **Colombia**: same pattern, commits
  `cinemark_co_data.json` + `schedule_cinemark_co/` + `dbox_theatres_cache_co.json`.
  Secret: `DISPATCH_PAT` (shared); `CINEMARK_CO_PROXY` / `CINEMARK_CO_TOKEN` optional.

Each is independent (own `repository_dispatch` event + concurrency group). Start each
once from the Actions tab. Adding a new country = drop in another `live-loop-<cc>.yml`.

## Status

- [x] Scraper: `discover` + `measure`, proxy-aware; parsers verified on real-format fixtures.
- [x] Dashboard page (`dashboard/index.html`), logic verified against sample data.
- [x] Self-running GitHub Actions workflow + setup doc.
- [x] Colombia scraper (`cinemark_co_scraper.py`) + workflow + dashboard toggle; seat-plan
      and session parsers verified on a fixture captured from the live API.
- [ ] Add the repo secrets, enable Pages, and start each loop (one-time, see setup doc).
- [ ] First live end-to-end run per country (will populate the real `cinemark_*_data.json`).
