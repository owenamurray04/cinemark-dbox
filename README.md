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
/d-box-theatres            -> list of theatre pages that have D-BOX
/theatres/<slug>           -> that theatre's showtimes (HTML); keep linked = D-BOX
/TicketSeatMap/?...&LinkedShowtimeId=...
                           -> every seat as a <button>; count D-BOX sold
```

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

## Dashboard

`dashboard/index.html` is a standalone page (Cinemark red, to distinguish it from
the Cineplex board) that reads `dashboard/cinemark_data.json`: realized D-BOX
sell-through headline, D-BOX-vs-rest-of-house comparison + trend, an upcoming rail,
and a sortable table of completed showings. Serve the `dashboard/` folder via
GitHub Pages (see PROXY_AND_ACTIONS_SETUP.md).

## Automation

`.github/workflows/live-loop.yml` is a self-chaining loop (same design as the
Cineplex tracker): each short run does one discover-if-stale + measure, commits
`dashboard/cinemark_data.json` + `schedule_cinemark/`, then schedules the next run.
Needs two repo secrets — `CINEMARK_PROXY` (US residential) and `DISPATCH_PAT`.
Start it once from the Actions tab.

## Status

- [x] Scraper: `discover` + `measure`, proxy-aware; parsers verified on real-format fixtures.
- [x] Dashboard page (`dashboard/index.html`), logic verified against sample data.
- [x] Self-running GitHub Actions workflow + setup doc.
- [ ] Add the two repo secrets, enable Pages, and start the loop (one-time, see setup doc).
- [ ] First live end-to-end run (will populate real `cinemark_data.json`).
