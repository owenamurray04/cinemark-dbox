#!/usr/bin/env python3
"""
Cinemark D-BOX scraper — realized D-BOX sell-through across the US chain.

Reverse-engineered from cinemark.com (June 2026). Unlike Cineplex (a JSON API),
Cinemark server-renders everything into HTML, so we parse pages directly:

  1. D-BOX roster     /d-box-theatres                     -> theatre page slugs
  2. theatre page     /theatres/<slug>                    -> today's showtimes (HTML)
  3. seat map         /TicketSeatMap/?TheaterId&ShowtimeId&CinemarkMovieId&Showtime&LinkedShowtimeId
                                                          -> every seat as a <button>

HOW D-BOX WORKS ON CINEMARK (the crux)
  A D-BOX screening is sold as a PAIR of linked showtimes in ONE auditorium:
    - the regular seats   under the PRIMARY ShowtimeId   (seatType="seat")
    - the D-BOX seats      under the LINKED  ShowtimeId   (seatType="dbox")
  The seat map only includes the D-BOX seats when the request carries
  &LinkedShowtimeId=<linked>. So:
    * DISCOVERY: a showtime is "D-BOX" iff its TicketSeatMap link has a
      LinkedShowtimeId. (Confirmed again at measure time: we skip anything whose
      seat map has no seatType="dbox" seats.)
    * MEASUREMENT: fetch the seat map WITH the LinkedShowtimeId, then split
      seatType="dbox" (D-BOX) vs everything else (rest-of-house), counting
      available="False" as SOLD. Same realized-sell-through model as Cineplex.

PIPELINE
  roster -> showtimes per theatre (keep linked/D-BOX) -> seat map per D-BOX
  showing -> count D-BOX seats sold -> aggregate -> realized D-BOX sell-through.

Run live (from a US residential connection — Cinemark blocks datacenter IPs):
    python cinemark_scraper.py discover --date 6/22/2026
    python cinemark_scraper.py measure  --date 6/22/2026
Demo on the bundled fixtures (no network):
    python cinemark_scraper.py demo
"""
import argparse
import gzip
import json
import os
import random
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from http.cookiejar import CookieJar
from urllib.parse import quote, urljoin

try:
    import zoneinfo  # py3.9+: convert naive local showtimes -> UTC per theatre tz
except Exception:  # pragma: no cover
    zoneinfo = None

BASE = "https://www.cinemark.com"
HEADERS = {
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "accept-language": "en-US,en;q=0.9",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"),
}
# Polite, jittered pause between requests so traffic isn't a robotic heartbeat.
DELAY_RANGE_S = (1.1, 3.2)

# Minutes after a show's start before we "lock it in" as realized (= completed).
# Must match the dashboard's REALIZE_MS so a show never falls out of the upcoming
# rail before it appears under completed.
REALIZE_MIN = 15


# ---- residential proxy + session cookies -----------------------------------
# Cinemark 403s/blanks datacenter IPs (incl. GitHub Actions runners). Route every
# request through a US residential proxy by setting CINEMARK_PROXY, e.g.
#   export CINEMARK_PROXY="http://USER:PASS@gw.dataimpulse.com:823"
# (DataImpulse: append a US country code to the username, e.g. USER__cr.us).
# If unset, falls back to HTTPS_PROXY / direct (works from a US machine/VPN).
PROXY = (os.environ.get("CINEMARK_PROXY") or os.environ.get("HTTPS_PROXY")
         or os.environ.get("https_proxy") or "").strip() or None


def _load_cookie():
    """Read cookie.txt (raw Cookie string OR a pasted `Copy as cURL`) if present.
    The seat-map endpoint needs a warmed session; normally we get that by hitting
    a page first, but a hand-captured cookie is a reliable local fallback."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cinemark_cookie.txt")
    if not os.path.exists(path):
        return None
    text = open(path, encoding="utf-8").read()
    m = re.search(r"-b '([^']*)'", text) or re.search(r'-b "([^"]*)"', text)
    return (m.group(1) if m else text).strip() or None


COOKIE = _load_cookie()


def _build_opener():
    handlers = [urllib.request.HTTPCookieProcessor(CookieJar())]
    if PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # ignore env proxies
    return urllib.request.build_opener(*handlers)


_OPENER = _build_opener()
_SESSION_WARM = False
_WARNED_403 = False  # print the datacenter-block hint at most once per run
_WARNED_429 = False  # print the rate-limit hint at most once per run


def warm_session():
    """Prime the cookie jar by fetching the home page once, so subsequent
    seat-map requests carry a valid session (the seat map returns an empty body
    to a cold, cookieless client)."""
    global _SESSION_WARM
    if _SESSION_WARM or COOKIE:
        _SESSION_WARM = True
        return
    try:
        _get(BASE + "/", referer=None, tries=2)
    except Exception:
        pass
    _SESSION_WARM = True


def _get(url, referer=BASE + "/", tries=3):
    """GET a page, returning the body text (str) or None on persistent failure.
    Empty bodies / transient throttles are retried with backoff.

    Requests gzip and decompresses the response: the seat-map page is ~340 KB of
    HTML uncompressed but ~6-8x smaller gzipped, and the proxy bills by bytes over
    the wire — so this cuts bandwidth (and cost) massively with zero loss."""
    headers = dict(HEADERS)
    headers["accept-encoding"] = "gzip"
    if referer:
        headers["referer"] = referer
    if COOKIE:
        headers["Cookie"] = COOKIE
    for attempt in range(tries):
        time.sleep(random.uniform(*DELAY_RANGE_S))
        req = urllib.request.Request(url, headers=headers)
        try:
            with _OPENER.open(req, timeout=30) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc or raw[:2] == b"\x1f\x8b":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass  # not actually gzipped — use as-is
            body = raw.decode("utf-8", "replace").strip()
            if not body:
                time.sleep(1.0 * (attempt + 1))  # maybe throttled — back off
                continue
            return body
        except urllib.error.HTTPError as e:
            if e.code == 403:
                if attempt < tries - 1:
                    time.sleep(1.5 * (attempt + 1))  # transient / rotate exit IP
                    continue
                # Exhausted retries. Don't crash the whole run — degrade gracefully
                # so a datacenter-IP block shows up as "no data" in the logs (the
                # signal to re-enable CINEMARK_PROXY) instead of a hard failure.
                global _WARNED_403
                if not _WARNED_403:
                    _WARNED_403 = True
                    print("  [warn] HTTP 403 from Cinemark — likely a datacenter-IP block. "
                          "Running without a proxy; re-enable CINEMARK_PROXY if data stops.")
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2.0 * (attempt + 1))  # rate-limit / transient — back off
                continue
            # Exhausted retries (or a non-retryable code): degrade gracefully
            # rather than crash the long-running loop. 429 = rate limited (usually
            # means we're hammering a single IP — a proxy that rotates IPs avoids it).
            global _WARNED_429
            if e.code == 429 and not _WARNED_429:
                _WARNED_429 = True
                print("  [warn] HTTP 429 (rate limited) from Cinemark — too many requests "
                      "from one IP. A rotating residential proxy (CINEMARK_PROXY) avoids this.")
            return None
        except (ValueError, urllib.error.URLError):
            if attempt < tries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None
    return None


# ---- timezone: naive local showtime + US state -> aware UTC ----------------
# Theatre showtimes are local and have no offset; a UTC runner must localize per
# theatre before deciding which showings are "near now". Most US states sit in one
# zone; the handful that split are mapped to their majority/most-Cinemark zone.
_STATE_TZ = {
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "IN": "America/Indiana/Indianapolis",
    "ME": "America/New_York", "MD": "America/New_York", "MA": "America/New_York",
    "MI": "America/Detroit", "NH": "America/New_York", "NJ": "America/New_York",
    "NY": "America/New_York", "NC": "America/New_York", "OH": "America/New_York",
    "PA": "America/New_York", "RI": "America/New_York", "SC": "America/New_York",
    "VT": "America/New_York", "VA": "America/New_York", "WV": "America/New_York",
    "AL": "America/Chicago", "AR": "America/Chicago", "IL": "America/Chicago",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/New_York",
    "LA": "America/Chicago", "MN": "America/Chicago", "MS": "America/Chicago",
    "MO": "America/Chicago", "NE": "America/Chicago", "ND": "America/Chicago",
    "OK": "America/Chicago", "SD": "America/Chicago", "TN": "America/Chicago",
    "TX": "America/Chicago", "WI": "America/Chicago",
    "AZ": "America/Phoenix", "CO": "America/Denver", "ID": "America/Boise",
    "MT": "America/Denver", "NM": "America/Denver", "UT": "America/Denver",
    "WY": "America/Denver", "NV": "America/Los_Angeles", "CA": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles",
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}
_DEFAULT_TZ = "America/Chicago"  # Cinemark HQ (Plano, TX) — sensible fallback


def _tz_for(state):
    if zoneinfo is None:
        return timezone(timedelta(hours=-6))  # CT-ish fallback if no zoneinfo
    try:
        return zoneinfo.ZoneInfo(_STATE_TZ.get((state or "").upper(), _DEFAULT_TZ))
    except Exception:
        return zoneinfo.ZoneInfo(_DEFAULT_TZ)


def _start_utc(start_str, state=None):
    """'2026-06-22T22:25:00' (local to `state`) -> aware UTC datetime, or None."""
    try:
        naive = datetime.strptime((start_str or "")[:16], "%Y-%m-%dT%H:%M")
    except Exception:
        return None
    try:
        return naive.replace(tzinfo=_tz_for(state)).astimezone(timezone.utc)
    except Exception:
        return None


# ---- 1. theatre roster (the WHOLE US chain, from the sitemap) ---------------
# IMPORTANT: cinemark.com/d-box-theatres is location-filtered (client-side) and
# only returns a default ~38 theatres — NOT the national D-BOX list. The complete
# US theatre list lives in the (gzipped) sitemap. We pull every open theatre from
# it and detect D-BOX per theatre at scan time (a theatre with no linked showtimes
# simply contributes nothing). A cache (dbox_theatres_cache.json) remembers which
# theatres actually have D-BOX so only an occasional full pass is slow.
_SITEMAP_THEATRE = re.compile(r'<loc>\s*([^<]*?/theatres/([a-z]{2})-[^<]+?)\s*</loc>', re.I)
_CLOSED_RE = re.compile(r'now-closed|coming-soon', re.I)


def _name_from_slug(slug):
    """'/theatres/tx-the-woodlands/cinemark-the-woodlands-and-xd' ->
    'Cinemark The Woodlands and XD'."""
    last = (slug or "").rstrip("/").split("/")[-1]
    fixups = {"xd": "XD", "imax": "IMAX", "screenx": "ScreenX", "and": "and",
              "3d": "3D", "4dx": "4DX", "ii": "II", "iii": "III", "nextgen": "NextGen"}
    return " ".join(fixups.get(w.lower(), w.capitalize())
                    for w in last.replace("-", " ").split())


def _get_bytes(url, tries=3):
    """GET raw bytes (gunzipping if needed). Used for the gzipped sitemap."""
    headers = dict(HEADERS)
    if COOKIE:
        headers["Cookie"] = COOKIE
    for attempt in range(tries):
        time.sleep(random.uniform(*DELAY_RANGE_S))
        req = urllib.request.Request(url, headers=headers)
        try:
            with _OPENER.open(req, timeout=40) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":  # gzip magic
                raw = gzip.decompress(raw)
            if not raw:
                time.sleep(1.0 * (attempt + 1)); continue
            return raw
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1)); continue
            return None
        except (ValueError, urllib.error.URLError, OSError):
            if attempt < tries - 1:
                time.sleep(1.0 * (attempt + 1)); continue
            return None
    return None


def get_all_theatres():
    """Every OPEN Cinemark US theatre, from the sitemap. Returns
    [{slug, state, theatre}], state derived from the URL slug."""
    raw = _get_bytes(BASE + "/sitemap.xml")
    if not raw:
        return []
    text = raw.decode("utf-8", "replace")
    seen, out = set(), []
    for full, state in _SITEMAP_THEATRE.findall(text):
        slug = full.split("cinemark.com")[-1]  # path only
        if not slug.startswith("/theatres/") or slug in seen:
            continue
        if _CLOSED_RE.search(slug):
            continue  # skip permanently closed / not-yet-open locations
        seen.add(slug)
        out.append({"slug": slug, "state": state.upper(), "theatre": _name_from_slug(slug)})
    return out


# ---- D-BOX theatre cache: which theatres actually have D-BOX ----------------
def _cache_path():
    return _here("dbox_theatres_cache.json")


def _load_dbox_cache():
    try:
        return json.load(open(_cache_path()))
    except Exception:
        return {}


def _save_dbox_cache(slugs, theatres_by_slug):
    data = {"updatedUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(slugs),
            "theatres": [theatres_by_slug[s] for s in slugs if s in theatres_by_slug]}
    json.dump(data, open(_cache_path(), "w"), indent=2)


# ---- 2. showtimes (parse D-BOX showings off a theatre page) -----------------
# Every showtime is an <a href="/TicketSeatMap/?TheaterId=..&ShowtimeId=..&
# CinemarkMovieId=..&Showtime=..[&LinkedShowtimeId=..]">. A LinkedShowtimeId marks
# the D-BOX pairing (regular primary + D-BOX linked) — that's our D-BOX signal.
_SEATMAP_LINK = re.compile(
    r'href="(/TicketSeatMap/?\?[^"]*?TheaterId=(\d+)[^"]*?ShowtimeId=(\d+)'
    r'[^"]*?CinemarkMovieId=(\d+)[^"]*?Showtime=([0-9T:\-]+)[^"]*?)"', re.I)
_LINKED_RE = re.compile(r'LinkedShowtimeId=(\d+)', re.I)
# Each film's block links to /movies/<slug> (poster, title, AND "Watch Trailer"
# all share the same slug). We derive the title from the SLUG, not the link text
# — the trailer link's text is literally "Trailer", which is what bit us before.
_MOVIE_SLUG = re.compile(r'/movies/([a-z0-9][a-z0-9\-]+)', re.I)
# Slug tokens that should be upper-cased rather than title-cased.
_SLUG_UPPER = {"xd", "imax", "3d", "4dx", "ii", "iii", "iv", "f1", "vs", "tron"}


def _title_from_slug(slug):
    """'f1-the-movie' -> 'F1 The Movie'; 'masters-of-the-universe' ->
    'Masters of the Universe'. Best-effort, used only for grouping/display."""
    small = {"of", "the", "and", "a", "an", "to", "in", "with", "for"}
    words = [w for w in (slug or "").split("-") if w]
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in _SLUG_UPPER:
            out.append(w.upper())
        elif lw in small and i != 0:
            out.append(lw)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def parse_theatre_showtimes(html, state=None, theatre=None):
    """Return D-BOX showings parsed from one theatre page. A showing is D-BOX iff
    its seat-map link carries a LinkedShowtimeId. De-duplicated by ShowtimeId."""
    if not html:
        return []
    # Map each movie's /movies/<slug> position to its title, so we can label each
    # showtime with the nearest preceding film.
    titles = [(m.start(), _title_from_slug(m.group(1))) for m in _MOVIE_SLUG.finditer(html)]

    def title_at(pos):
        name = None
        for start, t in titles:
            if start <= pos:
                name = t
            else:
                break
        return name

    out, seen = [], set()
    for m in _SEATMAP_LINK.finditer(html):
        href, tid, sid, mid, start = m.groups()
        linked = _LINKED_RE.search(href)
        if not linked:
            continue  # not a D-BOX (linked) showtime
        if sid in seen:
            continue
        seen.add(sid)
        out.append({
            "theatreId": int(tid),
            "theatre": theatre,
            "movie": title_at(m.start()) or f"movie {mid}",
            "movieId": int(mid),
            "showtimeId": int(sid),
            "linkedShowtimeId": int(linked.group(1)),
            "start": start[:19],
            "state": state,
        })
    return out


def _clean_text(s):
    if not s:
        return s
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace("&amp;", "&").replace("&#39;", "'").replace("&rsquo;", "’")


def get_dbox_showings(theatre_slug, state=None, theatre=None):
    html = _get(BASE + theatre_slug, referer=BASE + "/d-box-theatres")
    return parse_theatre_showtimes(html, state=state, theatre=theatre)


# ---- 3. seat map -> D-BOX vs rest-of-house ---------------------------------
# Each seat is a <button ... available="True|False" ... seatType="dbox|seat|
# wheelchair|companion" ... showtimeId="..." ...>. D-BOX seats only appear when
# the request includes &LinkedShowtimeId=<linked>.
_SEAT_BTN = re.compile(r'<button\b([^>]*\bseatType="[^"]*"[^>]*)>', re.I)


def _attr(btn, name):
    m = re.search(name + r'="([^"]*)"', btn, re.I)
    return m.group(1) if m else None


def summarize_seatmap(html):
    """Pure-ish: tally D-BOX vs regular seats from seat-map HTML. SOLD =
    available="False". Returns D-BOX stats with the rest-of-house comparison
    nested under 'regular', or None if no seats were found."""
    btns = _SEAT_BTN.findall(html or "")
    if not btns:
        return None
    dbox = {"total": 0, "sold": 0}
    reg = {"total": 0, "sold": 0}
    for b in btns:
        st = (_attr(b, "seatType") or "").lower()
        sold = (_attr(b, "available") or "").lower() == "false"
        bucket = dbox if st == "dbox" else reg
        bucket["total"] += 1
        if sold:
            bucket["sold"] += 1

    def finish(d):
        sellable = d["total"]  # Cinemark marks broken/blocked as no button
        return {"total": d["total"], "sellable": sellable, "sold": d["sold"],
                "available": sellable - d["sold"],
                "sell_through": d["sold"] / sellable if sellable else 0.0}

    out = finish(dbox)
    out["regular"] = finish(reg)
    return out


def seatmap_url(theatre_id, showtime_id, movie_id, start, linked_id):
    return (f"{BASE}/TicketSeatMap/?TheaterId={theatre_id}&ShowtimeId={showtime_id}"
            f"&CinemarkMovieId={movie_id}&Showtime={quote(start)}"
            f"&LinkedShowtimeId={linked_id}")


def measure_showing(s):
    """Fetch one D-BOX showing's seat map and return its D-BOX summary, or None."""
    warm_session()
    url = seatmap_url(s["theatreId"], s["showtimeId"], s["movieId"],
                      s["start"], s["linkedShowtimeId"])
    html = _get(url, referer=BASE + (s.get("slug") or "/"))
    if not html:
        return None
    m = summarize_seatmap(html)
    if not m or m["total"] <= 0:
        return None  # no D-BOX seats identified — skip (don't record a false 0)
    return m


# ---- schedule store --------------------------------------------------------
def _date_iso(mdy):
    """'6/22/2026' -> '2026-06-22'. Pass-through if already ISO/unknown."""
    try:
        return datetime.strptime(mdy, "%m/%d/%Y").strftime("%Y-%m-%d")
    except Exception:
        return mdy


def _next_day_mdy(date_mdy):
    try:
        d = datetime.strptime(_date_iso(date_mdy), "%Y-%m-%d") + timedelta(days=1)
        return f"{d.month}/{d.day}/{d.year}"
    except Exception:
        return None


def _prev_day_mdy(date_mdy):
    try:
        d = datetime.strptime(_date_iso(date_mdy), "%Y-%m-%d") - timedelta(days=1)
        return f"{d.month}/{d.day}/{d.year}"
    except Exception:
        return None


def _here(*parts):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)


def _schedule_path(date_mdy):
    return _here("schedule_cinemark", f"{_date_iso(date_mdy)}.json")


def _save_schedule(date_mdy, showings):
    p = _schedule_path(date_mdy)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({"date": _date_iso(date_mdy),
               "builtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "count": len(showings), "showings": showings},
              open(p, "w"), indent=2)


def _load_schedule(date_mdy):
    p = _schedule_path(date_mdy)
    if not os.path.exists(p):
        return []
    try:
        return json.load(open(p)).get("showings", [])
    except Exception:
        return []


# ---- discover --------------------------------------------------------------
# How often to re-scan the WHOLE chain (vs just the cached D-BOX theatres) to
# pick up new D-BOX rollouts. Between full passes, discovery only scans theatres
# already known to have D-BOX, which is much faster and lighter on Cinemark.
CACHE_FULL_RESCAN_DAYS = 7


def discover(date_mdy, max_age_min=None, full=False):
    """Record the day's D-BOX showings across the US chain. Pulls the theatre list
    from the sitemap (all open theatres) on a full pass; between full passes only
    re-scans theatres already known to have D-BOX (cached). One theatre-page fetch
    per theatre, no seat-map calls. Skips entirely if today's schedule is fresh."""
    if max_age_min is not None:
        p = _schedule_path(date_mdy)
        if os.path.exists(p):
            try:
                built = json.load(open(p)).get("builtUtc")
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(built)).total_seconds() / 60
                existing = _load_schedule(date_mdy)
                if age < max_age_min and len(existing) >= 20:
                    print(f"[discover] schedule is {age:.0f} min old with "
                          f"{len(existing)} showings — skipping scan.")
                    return existing
            except Exception:
                pass

    warm_session()

    # Decide full-chain vs cached-D-BOX-only scan.
    cache = _load_dbox_cache()
    cache_age_days = None
    if cache.get("updatedUtc"):
        try:
            cache_age_days = (datetime.now(timezone.utc)
                              - datetime.fromisoformat(cache["updatedUtc"])).total_seconds() / 86400
        except Exception:
            cache_age_days = None
    do_full = (full or not cache.get("theatres")
               or cache_age_days is None or cache_age_days >= CACHE_FULL_RESCAN_DAYS)

    if do_full:
        theatres = get_all_theatres()
        print(f"[discover] FULL chain scan: {len(theatres)} open US theatres for D-BOX on {date_mdy}")
    else:
        theatres = cache.get("theatres", [])
        print(f"[discover] cached scan: {len(theatres)} known D-BOX theatres "
              f"(cache {cache_age_days:.1f}d old) on {date_mdy}")

    want_iso = _date_iso(date_mdy)
    showings, dbox_slugs, by_slug = [], [], {}
    for i, t in enumerate(theatres):
        by_slug[t["slug"]] = t
        rows = get_dbox_showings(t["slug"], state=t["state"], theatre=t["theatre"])
        if rows:
            dbox_slugs.append(t["slug"])  # theatre offers D-BOX -> keep in cache
        for r in rows:
            if (r.get("start") or "")[:10] != want_iso:
                continue  # the page renders today by default; keep matching date
            r["slug"] = t["slug"]
            showings.append(r)
        if do_full and (i + 1) % 50 == 0:
            print(f"  ...scanned {i + 1}/{len(theatres)} theatres, "
                  f"{len(dbox_slugs)} with D-BOX so far")

    # Guard a failed/partial scan from poisoning the day.
    existing = _load_schedule(date_mdy)
    if not showings or (existing and len(showings) < 0.5 * len(existing)):
        print(f"[discover] scan returned {len(showings)} showings"
              + (f" vs {len(existing)} already listed" if existing else "")
              + " — looks like a hiccup; keeping existing schedule.")
        return existing

    _save_schedule(date_mdy, showings)
    # After a full pass, refresh the cache so the next runs are fast & light.
    if do_full and dbox_slugs:
        _save_dbox_cache(dbox_slugs, by_slug)
        print(f"[discover] cached {len(dbox_slugs)} D-BOX theatres -> {_cache_path()}")
    n_th = len({s["theatreId"] for s in showings})
    n_mv = len({s["movie"] for s in showings})
    print(f"[discover] {len(showings)} D-BOX showings across {n_th} theatres, "
          f"{n_mv} titles -> {_schedule_path(date_mdy)}")
    return showings


# ---- measure (near-showtime) ----------------------------------------------
def measure_window(date_mdy, lead_min=30, grace_min=20):
    """Measure only D-BOX showings whose start is within [now-grace, now+lead].
    Runs every ~15 min so each show is read a few times as it approaches start;
    the realized store keeps the fullest reading and locks it in after start."""
    sched = _load_schedule(date_mdy)
    if not sched:
        print("[measure] no schedule for today yet — running discovery first.")
        sched = discover(date_mdy)
    # Fold in yesterday's schedule too: a late west-coast show plays after the ET
    # date has rolled over.
    prev = _load_schedule(_prev_day_mdy(date_mdy))
    if prev:
        seen = {(s.get("theatreId"), s.get("showtimeId")) for s in sched}
        sched = sched + [s for s in prev
                         if (s.get("theatreId"), s.get("showtimeId")) not in seen]

    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(minutes=grace_min), now + timedelta(minutes=lead_min)

    store = {}
    try:
        store = (json.load(open(_data_path())).get("showingsStore") or {})
    except Exception:
        store = {}

    due, skipped_final = [], 0
    for s in sched:
        st = _start_utc(s.get("start"), s.get("state"))
        if not (st and lo <= st <= hi):
            continue
        key = f"{(s.get('start') or '')[:10]}|{s.get('theatreId')}|{s.get('showtimeId')}"
        prev_rec = store.get(key)
        if prev_rec and prev_rec.get("lastSeenUtc"):
            try:
                if datetime.fromisoformat(prev_rec["lastSeenUtc"]) >= st + timedelta(minutes=10):
                    skipped_final += 1
                    continue
            except Exception:
                pass
        due.append((st, s))
    due.sort(key=lambda x: x[0])
    print(f"[measure] {len(due)} D-BOX showings to read in window "
          f"[{lo:%H:%M}..{hi:%H:%M} UTC] of {len(sched)} scheduled "
          f"({skipped_final} already finalized, skipped)")

    rows = []
    for _, s in due:
        m = measure_showing(s)
        if not m:
            continue
        rows.append({**s, **m})
        print(f"  {str(s.get('movie'))[:24]:24} {(s.get('start') or '')[11:16]} "
              f"T{s.get('theatreId')} D-BOX {m['sold']:2}/{m['sellable']:2} "
              f"({m['sell_through']:.0%})  reg {m['regular']['sold']}/{m['regular']['sellable']}")

    _write_dashboard_data(rows, scrape_date=date_mdy)

    # Recommend the next wait, from the actual schedule.
    LO, HI, CAP = 4 * 60, 90 * 60, 90 * 60
    future = [st for s in sched
              if (st := _start_utc(s.get("start"), s.get("state"))) and st > now]
    if due:
        wait = 5 * 60 + random.randint(-30, 75)
    elif future:
        wait = (min(future) - now).total_seconds() - (lead_min + 5) * 60
        wait = min(max(wait, LO), HI) + random.randint(-45, 120)
    else:
        wait = 75 * 60 + random.randint(0, 600)
    wait = int(min(max(wait, LO), CAP))
    print(f"MEASURED={len(rows)}")
    print(f"NEXT_WAIT_S={wait}")
    return rows


# ---- dashboard data store (separate file; does NOT touch the Cineplex one) ---
_TITLE_CANON = {}  # extend if Cinemark ever splits a title across releases


def _canon_title(name):
    if not name:
        return name
    key = "".join(c for c in unicodedata.normalize("NFD", name.strip().lower())
                  if unicodedata.category(c) != "Mn")
    key = key.replace("'", "").replace("’", "").replace(":", "").strip()
    key = re.sub(r"\s+", " ", key)
    return _TITLE_CANON.get(key, name)


def _data_path():
    return _here("dashboard", "cinemark_data.json")


def _write_dashboard_data(rows, scrape_date=None, quiet=False):
    """Accumulate per-showing D-BOX occupancy across runs into cinemark_data.json.
    Headline metric is REALIZED sell-through: how full each D-BOX showing actually
    got, counted only once its start time has passed. Mirrors the Cineplex store."""
    path = _data_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat(timespec="seconds")
    now_local = datetime.now()

    data = {}
    if os.path.exists(path):
        try:
            data = json.load(open(path))
        except Exception:
            data = {}
    store = data.get("showingsStore") or {}

    for r in rows:
        start = r.get("start") or ""
        sdate = start[:10] or (scrape_date and _date_iso(scrape_date)) or now_local.strftime("%Y-%m-%d")
        key = f"{sdate}|{r.get('theatreId')}|{r.get('showtimeId')}"
        prev = store.get(key)
        sold, sellable = r.get("sold", 0), r.get("sellable", 0)
        reg = r.get("regular") or {}
        if prev and prev.get("sold", 0) >= sold:
            prev["sellable"] = max(prev.get("sellable", 0), sellable)
            prev["lastSeenUtc"] = now_iso
            if reg.get("sellable"):
                prev["regSold"] = reg.get("sold", 0)
                prev["regSellable"] = reg.get("sellable", 0)
            continue
        store[key] = {
            "date": sdate, "theatreId": r.get("theatreId"),
            "theatre": r.get("theatre") or (prev or {}).get("theatre"),
            "movie": r.get("movie"),
            "showtimeId": r.get("showtimeId"), "linkedShowtimeId": r.get("linkedShowtimeId"),
            "movieId": r.get("movieId"), "start": start, "state": r.get("state"),
            "sold": sold, "sellable": sellable,
            "regSold": reg.get("sold", (prev or {}).get("regSold", 0)),
            "regSellable": reg.get("sellable", (prev or {}).get("regSellable", 0)),
            "firstSeenUtc": (prev or {}).get("firstSeenUtc", now_iso), "lastSeenUtc": now_iso,
        }

    for rec in store.values():
        if not rec.get("startUtc"):
            su = _start_utc(rec.get("start"), rec.get("state"))
            rec["startUtc"] = su.isoformat() if su else None
        if not rec.get("cinemarkUrl") and rec.get("linkedShowtimeId") and rec.get("movieId"):
            rec["cinemarkUrl"] = seatmap_url(rec.get("theatreId"), rec.get("showtimeId"),
                                             rec.get("movieId"), rec.get("start") or "",
                                             rec.get("linkedShowtimeId"))

    def is_realized(rec):
        sutc = _start_utc(rec.get("start"), rec.get("state"))
        if sutc is not None:
            return sutc + timedelta(minutes=REALIZE_MIN) <= now_utc
        return False

    finals, upcoming = [], []
    for rec in store.values():
        if (rec.get("sellable") or 0) <= 0:
            continue
        (finals if is_realized(rec) else upcoming).append(rec)

    def _agg(recs):
        sold = sum(x.get("sold", 0) for x in recs)
        sell = sum(x.get("sellable", 0) for x in recs)
        return {"seatsSold": sold, "seatsSellable": sell,
                "sellThrough": round(sold / sell, 4) if sell else 0.0,
                "showings": len(recs), "theatres": len({x.get("theatreId") for x in recs})}

    realized = _agg(finals)

    cmp_recs = [x for x in finals if (x.get("regSellable") or 0) > 0]
    _ds = sum(x.get("sold", 0) for x in cmp_recs); _dse = sum(x.get("sellable", 0) for x in cmp_recs)
    _rs = sum(x.get("regSold", 0) for x in cmp_recs); _rse = sum(x.get("regSellable", 0) for x in cmp_recs)
    comparison = {
        "showings": len(cmp_recs), "dboxSold": _ds, "dboxSellable": _dse,
        "regularSold": _rs, "regularSellable": _rse,
        "dboxSellThrough": round(_ds / _dse, 4) if _dse else 0.0,
        "regularSellThrough": round(_rs / _rse, 4) if _rse else 0.0,
    }

    bym = {}
    for x in finals:
        title = _canon_title(x.get("movie"))
        m = bym.setdefault(title, {"movie": title, "sold": 0, "sellable": 0, "showings": 0})
        m["sold"] += x.get("sold", 0); m["sellable"] += x.get("sellable", 0); m["showings"] += 1
    by_movie = sorted(
        ({**m, "sellThrough": round(m["sold"] / m["sellable"], 4) if m["sellable"] else 0.0}
         for m in bym.values()), key=lambda x: -x["sellThrough"])

    byd = {}
    for x in finals:
        d = byd.setdefault(x["date"], {"date": x["date"], "sold": 0, "sellable": 0, "showings": 0})
        d["sold"] += x.get("sold", 0); d["sellable"] += x.get("sellable", 0); d["showings"] += 1
    by_day = sorted(
        ({**d, "sellThrough": round(d["sold"] / d["sellable"], 4) if d["sellable"] else 0.0}
         for d in byd.values()), key=lambda x: x["date"])

    def _rows(recs):
        out = []
        for x in recs:
            row = {"theatreId": x.get("theatreId"), "theatre": x.get("theatre"),
                   "movie": _canon_title(x.get("movie")),
                   "date": x.get("date"), "start": (x.get("start") or "")[11:16],
                   "startUtc": x.get("startUtc"), "state": x.get("state"),
                   "cinemarkUrl": x.get("cinemarkUrl"), "sold": x.get("sold", 0),
                   "sellable": x.get("sellable", 0), "lastSeenUtc": x.get("lastSeenUtc"),
                   "sellThrough": round(x["sold"] / x["sellable"], 4) if x.get("sellable") else 0.0}
            if (x.get("regSellable") or 0) > 0:
                row["regSold"] = x.get("regSold", 0)
                row["regSellable"] = x.get("regSellable", 0)
                row["regSellThrough"] = round(x["regSold"] / x["regSellable"], 4)
            out.append(row)
        return out

    completed = sorted(finals, key=lambda x: (x.get("start") or ""), reverse=True)[:80]
    recent = sorted(finals + upcoming, key=lambda x: (x.get("lastSeenUtc") or ""), reverse=True)[:24]
    upnext = sorted(upcoming, key=lambda x: (x.get("start") or ""))[:60]

    # Upcoming D-BOX showings straight from the schedule (today + tomorrow), not
    # just the ones we've already measured — so the dashboard rail shows everything
    # still to play. Measured fill is merged in from the store where we have it.
    upcoming_sched = []
    if scrape_date:
        seen_up = set()
        for dmdy in (scrape_date, _next_day_mdy(scrape_date)):
            for s in _load_schedule(dmdy):
                tid, sid = s.get("theatreId"), s.get("showtimeId")
                if (tid, sid) in seen_up:
                    continue
                su = _start_utc(s.get("start"), s.get("state"))
                if not su or su + timedelta(minutes=REALIZE_MIN) <= now_utc:
                    continue  # keep in the rail until it's realized (moves to completed)
                seen_up.add((tid, sid))
                rec = store.get(f"{(s.get('start') or '')[:10]}|{tid}|{sid}") or {}
                upcoming_sched.append({
                    "theatre": s.get("theatre"), "theatreId": tid,
                    "movie": _canon_title(s.get("movie")), "start": s.get("start"),
                    "startUtc": su.isoformat(),
                    "cinemarkUrl": seatmap_url(tid, sid, s.get("movieId"),
                                               s.get("start") or "", s.get("linkedShowtimeId")),
                    "sold": rec.get("sold"), "sellable": rec.get("sellable"),
                    "lastSeenUtc": rec.get("lastSeenUtc"),
                })
        upcoming_sched.sort(key=lambda x: x["startUtc"])
        upcoming_sched = upcoming_sched[:150]

    data = {
        "updatedUtc": now_iso,
        "source": "Cinemark · D-BOX motion seats",
        "metric": "realized",
        "showingsStore": store,
        "upcoming": upcoming_sched,
        "byDay": by_day,
        "latest": {
            "scrapeDate": _date_iso(scrape_date) if scrape_date else now_local.strftime("%Y-%m-%d"),
            "sellThrough": realized["sellThrough"],
            "seatsSold": realized["seatsSold"], "seatsSellable": realized["seatsSellable"],
            "showings": realized["showings"], "theatres": realized["theatres"],
            "upcoming": _agg(upcoming),
            "comparison": comparison,
            "byMovie": by_movie,
            "showingsList": _rows(completed),
            "recentFills": _rows(recent),
            "upcomingList": _rows(upnext),
        },
    }
    json.dump(data, open(path, "w"), indent=2)
    if quiet:
        return
    up = _agg(upcoming)
    print(f"  dashboard data -> {path}")
    print(f"  REALIZED: {realized['seatsSold']}/{realized['seatsSellable']} D-BOX seats sold "
          f"across {realized['showings']} completed showings "
          f"({realized['sellThrough']:.0%})")
    print(f"  ({up['showings']} more measured but not yet played — they count once they do)")


# ---- demo on bundled fixtures (no network) ---------------------------------
def demo():
    here = _here("fixtures", "cinemark")
    print("STAGE 1 — DISCOVERY  (real theatre-page HTML, parse D-BOX showings)")
    print("-" * 64)
    page = open(os.path.join(here, "theatre_page.html"), encoding="utf-8").read()
    showings = parse_theatre_showtimes(page, state="TX", theatre="Cinemark The Woodlands and XD")
    print(f"  D-BOX showings found: {len(showings)}")
    for s in showings[:6]:
        print(f"  D-BOX -> {s['movie'][:24]:24} {s['start'][11:16]} "
              f"showtime={s['showtimeId']} linked={s['linkedShowtimeId']}")

    print("\nSTAGE 2 — MEASUREMENT  (real seat-map HTML, split D-BOX vs rest-of-house)")
    print("-" * 64)
    smap = open(os.path.join(here, "seatmap_dbox.html"), encoding="utf-8").read()
    m = summarize_seatmap(smap)
    print(f"  D-BOX seats: {m['total']}  SOLD: {m['sold']}  open: {m['available']}")
    print(f"  >> D-BOX SELL-THROUGH: {m['sell_through']:.0%}")
    r = m["regular"]
    print(f"  rest-of-house: {r['sold']}/{r['sellable']} sold ({r['sell_through']:.0%})")
    print("\nLIVE, the two stages chain: each linked showtime from Stage 1 feeds a")
    print("Stage 2 seat-map call; summed across theatres = chain-wide D-BOX sell-through.")
    print("Run live only from a US residential IP, politely rate-limited.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="list today's D-BOX showings -> schedule_cinemark/<date>.json")
    d.add_argument("--date", required=True, help="M/D/YYYY")
    d.add_argument("--max-age", type=int, default=None,
                   help="skip the scan if today's schedule is younger than N minutes")
    d.add_argument("--full", action="store_true",
                   help="force a full-chain scan (all theatres) and refresh the D-BOX cache")

    m = sub.add_parser("measure", help="measure D-BOX showings near their start time")
    m.add_argument("--date", required=True, help="M/D/YYYY")
    m.add_argument("--lead", type=int, default=30,
                   help="measure shows starting within this many minutes from now")
    m.add_argument("--grace", type=int, default=20,
                   help="also measure shows that started up to this many minutes ago")

    sub.add_parser("demo")
    a = p.parse_args()
    if a.cmd == "discover":
        discover(a.date, max_age_min=a.max_age, full=a.full)
    elif a.cmd == "measure":
        measure_window(a.date, lead_min=a.lead, grace_min=a.grace)
    else:
        demo()
