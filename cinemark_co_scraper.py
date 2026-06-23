#!/usr/bin/env python3
"""
Cinemark COLOMBIA D-BOX scraper — realized D-BOX sell-through, same metric as the
US and Brazil trackers but against Cinemark's Latin-America "cinemark-core" stack.

Colombia (cinemark.com.co) is a client-rendered Next.js app backed by a Vista
"WSVistaWebClient" OData service, proxied through a clean JSON gateway at
api.cinemark-core.com. The ONLY auth it needs is a static, non-secret header
`connectapitoken: web-co-token` (per-country: web-pe-token, web-cl-token, ...) plus
an Origin/Referer — no login, no rotating token. Endpoints (all GET):

  base: https://api.cinemark-core.com/vista/country/co
  companyId (tenant): 5db771be04daec00076df3f5
  1. /theaters?$format=json
        -> OData Cinemas: {value:[{ID,Name,City,Address1,Latitude,Longitude,
           LoyaltyCode,TimeZoneId,...}]}  (the whole CO roster)
  2. /city/<citySlug>/movies-billboard-city            -> movies now playing in a city
     (or /movies-billboard for the whole country; /movie/<id>/get-cities-by-movie)
  3. /city/<citySlug>/movie/<movieId>?date=YYYY-MM-DD&companyId=<tenant>
       &midnightSessionStart=23:10&midnightSessionEnd=03:00
        -> {Theater:[{CinemaId,Name,Format:[{ScreenTypes,SeatTypes,LangTypes,
           Sessions:[{SessionId,Showtime,SeatsAvailable,ScreenNumber,...}]}]}]}
        A session is D-BOX  iff its Format.SeatTypes contains "DBOX".
  4. /cinemas/<cinemaId>/sessions/<sessionId>/seat-plan
        -> {SeatLayoutData:{AreaCategories:[{AreaCategoryCode,Name}],
            Areas:[{AreaCategoryCode,Description,NumberOfSeats,
                    Rows:[{Seats:[{Id,Status}]}]}]}}
        D-BOX seats  = the Area(s) whose AreaCategory Name (or Description) contains
        "DBOX".  Seat Status: 0=available, 1=SOLD, others (3=broken, 7=space/..)=not
        sellable.  So D-BOX sell-through = sold / (available+sold), with the
        non-D-BOX areas as the rest-of-house comparison — the SAME realized model as
        the US/Brazil trackers (two reads per showing: early + ~10 min after start).

    python cinemark_co_scraper.py discover --date 6/24/2026
    python cinemark_co_scraper.py measure  --date 6/24/2026
    python cinemark_co_scraper.py demo      # offline, bundled fixture
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

try:
    import zoneinfo
except Exception:  # pragma: no cover
    zoneinfo = None

# ---- per-country config (this file = Colombia; clone the dict for PE/CL/... ) ----
COUNTRY = "co"
TENANT = "5db771be04daec00076df3f5"          # companyId for Colombia
SITE = "https://www.cinemark.com.co"
API = f"https://api.cinemark-core.com/vista/country/{COUNTRY}"
# The connect token is a STATIC public string baked into every cinemark-core site.
# Overridable via env in case a country uses a different label.
TOKEN = (os.environ.get("CINEMARK_CO_TOKEN") or f"web-{COUNTRY}-token").strip()
# Colombia is single-zone (UTC-5, no DST). Other countries override _DEFAULT_TZ.
_DEFAULT_TZ = "America/Bogota"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-encoding": "gzip",
    "accept-language": "es-CO,es;q=0.9,en;q=0.8",
    "connectapitoken": TOKEN,
    "origin": SITE,
    "referer": SITE + "/",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"),
}
DELAY_RANGE_S = (0.8, 2.2)
REALIZE_MIN = 15

# Seat model (verified on a live Bogota D-BOX seat map):
ST_SOLD = {1}                 # Status 1 = Ocupado (sold)
ST_AVAILABLE = {0}            # Status 0 = available
# Sellable = available + sold; every other status (3=broken, 7=space, etc.) is
# excluded from the denominator, exactly like the US/BR "blocked isn't sellable".

# Two reads per showing (cost control), same as US/BR.
EARLY_READ_MIN = 45
FINAL_READ_AFTER_MIN = 10

# ---- proxy + session -------------------------------------------------------
PROXY = (os.environ.get("CINEMARK_CO_PROXY") or os.environ.get("HTTPS_PROXY")
         or os.environ.get("https_proxy") or "").strip() or None


def _build_opener():
    handlers = [urllib.request.HTTPCookieProcessor(CookieJar())]
    handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY} if PROXY else {}))
    return urllib.request.build_opener(*handlers)


_OPENER = _build_opener()
_WARNED = False


def _get_json(path, tries=3):
    """GET an API path -> parsed JSON (decompressing gzip), or None. `path` may be a
    full URL or an API-relative path (starting with '/')."""
    url = path if path.startswith("http") else (API + path)
    for attempt in range(tries):
        time.sleep(random.uniform(*DELAY_RANGE_S))
        try:
            with _OPENER.open(urllib.request.Request(url, headers=HEADERS), timeout=30) as r:
                raw = r.read()
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
            body = raw.decode("utf-8", "replace").strip()
            if not body:
                time.sleep(1.0 * (attempt + 1)); continue
            return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2.0 * (attempt + 1)); continue
            global _WARNED
            if e.code in (401, 403, 429) and not _WARNED:
                _WARNED = True
                print(f"  [warn] HTTP {e.code} from cinemark-core — check the "
                      f"connectapitoken ('{TOKEN}') / Origin, or set CINEMARK_CO_PROXY.")
            return None
        except (ValueError, urllib.error.URLError, OSError):
            if attempt < tries - 1:
                time.sleep(1.0 * (attempt + 1)); continue
            return None
    return None


def _data(j):
    """Unwrap the optional gateway envelope ({dataResult:...}) if present."""
    if isinstance(j, dict) and "dataResult" in j:
        return j.get("dataResult")
    return j


def _odata(j):
    """Unwrap an OData payload ({value:[...]}) or a bare list/envelope."""
    j = _data(j)
    if isinstance(j, dict) and "value" in j:
        return j.get("value") or []
    return j if isinstance(j, list) else []


# ---- helpers ---------------------------------------------------------------
def _norm(s):
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").strip().lower())
                   if unicodedata.category(c) != "Mn")


def _slugify(name):
    s = _norm(name)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or ""


def _here(*p):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *p)


def _tz_for(_state=None):
    if zoneinfo is None:
        return timezone(timedelta(hours=-5))  # Colombia UTC-5, no DST
    try:
        return zoneinfo.ZoneInfo(_DEFAULT_TZ)
    except Exception:
        return timezone(timedelta(hours=-5))


def _start_utc(start_str, state=None):
    """'2026-06-24T12:00:00' (local) -> aware UTC datetime, or None."""
    try:
        naive = datetime.strptime((start_str or "")[:16], "%Y-%m-%dT%H:%M")
    except Exception:
        return None
    try:
        return naive.replace(tzinfo=_tz_for(state)).astimezone(timezone.utc)
    except Exception:
        return None


# ---- 1. roster: every Colombian Cinemark + the city slugs it lives in -------
def get_cinemas():
    """All CO cinemas from the OData Cinemas feed. Returns
    {cinemaId: {theatre, city, citySlug}}."""
    rows = _odata(_get_json("/theaters?$format=json"))
    out = {}
    for c in rows:
        cid = str(c.get("ID") or c.get("Id") or c.get("CinemaId") or "")
        if not cid:
            continue
        city = c.get("City") or ""
        out[cid] = {"theatre": c.get("Name"), "city": city, "citySlug": _slugify(city)}
    return out


def get_city_slugs(cinemas):
    return sorted({c["citySlug"] for c in cinemas.values() if c.get("citySlug")})


# ---- 2. movies now playing (per city) --------------------------------------
def _movie_id(m):
    return str(m.get("CorporateFilmId") or m.get("ID") or m.get("Id")
               or m.get("HOFilmCode") or "")


def _movie_title(m):
    return (m.get("Title") or m.get("Name") or m.get("FilmTitle")
            or m.get("TitleAlt") or "")


def get_city_movies(city_slug):
    """Movies playing in a city -> [{movieId, title}] (de-duplicated)."""
    j = _get_json(f"/city/{city_slug}/movies-billboard-city?$format=json")
    rows = _odata(j) or (_data(j) if isinstance(_data(j), list) else [])
    out, seen = [], set()
    for m in rows or []:
        mid = _movie_id(m)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"movieId": mid, "title": _movie_title(m) or f"movie {mid}"})
    return out


# ---- 3. D-BOX sessions off the per-movie, per-city sessions feed ------------
def _has_dbox(seat_types):
    return any("DBOX" in str(s).upper() or "D-BOX" in str(s).upper()
               for s in (seat_types or []))


def parse_dbox_sessions(js, city=None, movie_id=None, title=None, want_iso=None):
    """Return D-BOX sessions from one /city/<slug>/movie/<id> payload. A session is
    D-BOX iff its Format's SeatTypes include DBOX. Showtime is a local time-of-day;
    we stamp it onto want_iso to get the full local start."""
    dr = _data(js) or {}
    theaters = dr.get("Theater") or dr.get("Theaters") or []
    out, seen = [], set()
    for t in theaters:
        cid = str(t.get("CinemaId") or t.get("ID") or "")
        for f in (t.get("Format") or []):
            if not _has_dbox(f.get("SeatTypes")):
                continue
            screen_types = "+".join(f.get("ScreenTypes") or [])
            for s in (f.get("Sessions") or []):
                sid = str(s.get("SessionId") or "")
                if not sid or sid in seen:
                    continue
                if s.get("IsVisible") is False:
                    continue
                seen.add(sid)
                tod = (s.get("Showtime") or "")[:8]          # 'HH:MM:SS'
                start = f"{want_iso}T{tod}" if want_iso and tod else ""
                out.append({
                    "cinemaId": cid, "theatre": t.get("Name"),
                    "city": city, "movie": title or f"movie {movie_id}",
                    "movieId": str(movie_id or ""), "sessionId": sid,
                    "screen": s.get("ScreenNumber"), "screenTypes": screen_types,
                    "seatsAvailable": s.get("SeatsAvailable"),
                    "start": start[:19],
                })
    return out


def get_dbox_sessions(city_slug, movie, want_iso):
    js = _get_json(f"/city/{city_slug}/movie/{movie['movieId']}?date={want_iso}"
                   f"&companyId={TENANT}&midnightSessionStart=23:10&midnightSessionEnd=03:00")
    return parse_dbox_sessions(js, city=city_slug, movie_id=movie["movieId"],
                               title=movie.get("title"), want_iso=want_iso)


# ---- 4. seat map -> D-BOX vs rest-of-house ---------------------------------
def _dbox_area_codes(sld):
    """Codes of the AreaCategories that are D-BOX (Name/Description contains DBOX)."""
    codes = set()
    for a in (sld.get("AreaCategories") or []):
        if "DBOX" in _norm(a.get("Name")).upper().replace("-", "") or "DBOX" in _norm(a.get("Name")).upper():
            codes.add(str(a.get("AreaCategoryCode")))
    # also fall back to per-area Description, in case categories aren't labelled
    for a in (sld.get("Areas") or []):
        desc = _norm(a.get("Description")).upper().replace("-", "")
        if "DBOX" in desc:
            codes.add(str(a.get("AreaCategoryCode")))
    return codes


def summarize_seatmap(seatmap_json):
    """Tally D-BOX vs rest-of-house from a seat-plan payload. SOLD = Status in
    ST_SOLD; sellable = Status in ST_SOLD|ST_AVAILABLE (everything else — broken,
    spaces — is excluded). Returns D-BOX stats with 'regular' nested, or None."""
    dr = _data(seatmap_json) or {}
    sld = dr.get("SeatLayoutData") or {}
    areas = sld.get("Areas")
    if not areas:
        return None
    dbox_codes = _dbox_area_codes(sld)

    def fresh():
        return {"total": 0, "sold": 0, "sellable": 0}

    dbox, reg = fresh(), fresh()
    for a in areas:
        bucket = dbox if str(a.get("AreaCategoryCode")) in dbox_codes else reg
        for row in (a.get("Rows") or []):
            for seat in (row.get("Seats") or []):
                if not seat or seat.get("Id") in (None, ""):
                    continue  # empty cell / spacing, not a real seat
                st = seat.get("Status")
                bucket["total"] += 1
                if st in ST_SOLD:
                    bucket["sold"] += 1; bucket["sellable"] += 1
                elif st in ST_AVAILABLE:
                    bucket["sellable"] += 1
                # else broken/space -> counted in total but not sellable

    def finish(d):
        return {"total": d["total"], "sellable": d["sellable"], "sold": d["sold"],
                "available": d["sellable"] - d["sold"],
                "sell_through": d["sold"] / d["sellable"] if d["sellable"] else 0.0}

    if dbox["total"] == 0:
        return None  # no D-BOX seats identified — skip (don't record a false 0)
    out = finish(dbox)
    out["regular"] = finish(reg)
    return out


def seatmap_url(cinema_id, session_id):
    return f"{API}/cinemas/{cinema_id}/sessions/{session_id}/seat-plan"


def cinemark_url(city_slug, movie_id, session_id):
    return f"{SITE}/pelicula/{movie_id}?sessionId={session_id}"


def measure_showing(s):
    js = _get_json(seatmap_url(s["cinemaId"], s["sessionId"]))
    if not js:
        return None
    m = summarize_seatmap(js)
    if not m or m["total"] <= 0:
        return None
    return m


# ---- schedule + store ------------------------------------------------------
def _date_iso(mdy):
    try:
        return datetime.strptime(mdy, "%m/%d/%Y").strftime("%Y-%m-%d")
    except Exception:
        return mdy


def _prev_day_mdy(mdy):
    try:
        d = datetime.strptime(_date_iso(mdy), "%Y-%m-%d") - timedelta(days=1)
        return f"{d.month}/{d.day}/{d.year}"
    except Exception:
        return None


def _next_day_mdy(mdy):
    try:
        d = datetime.strptime(_date_iso(mdy), "%Y-%m-%d") + timedelta(days=1)
        return f"{d.month}/{d.day}/{d.year}"
    except Exception:
        return None


def _sched_path(mdy):
    return _here("schedule_cinemark_co", f"{_date_iso(mdy)}.json")


def _save_schedule(mdy, rows):
    p = _sched_path(mdy)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({"date": _date_iso(mdy),
               "builtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "count": len(rows), "showings": rows}, open(p, "w"), indent=2)


def _load_schedule(mdy):
    p = _sched_path(mdy)
    if not os.path.exists(p):
        return []
    try:
        return json.load(open(p)).get("showings", [])
    except Exception:
        return []


def _cache_path():
    return _here("dbox_theatres_cache_co.json")


def _load_cache():
    try:
        return json.load(open(_cache_path()))
    except Exception:
        return {}


def _save_cache(city_slugs, cinemas):
    json.dump({"updatedUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "dboxCitySlugs": sorted(city_slugs),
               "cinemas": cinemas}, open(_cache_path(), "w"), indent=2)


CACHE_TTL_DAYS = 7


# ---- discover --------------------------------------------------------------
def discover(date_mdy, max_age_min=None, full=False):
    """Record the day's D-BOX showings across Colombia. Roster from /theaters, then
    per (citySlug, movie) sessions; keep only sessions whose SeatTypes include DBOX.
    Between full passes, only re-scans the city slugs already known to host D-BOX."""
    cache = _load_cache()
    if max_age_min is not None and not full:
        p = _sched_path(date_mdy)
        if os.path.exists(p):
            try:
                built = json.load(open(p)).get("builtUtc")
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(built)).total_seconds() / 60
                existing = _load_schedule(date_mdy)
                if age < max_age_min and len(existing) >= 3:
                    print(f"[discover-co] schedule {age:.0f} min old with {len(existing)} showings — skipping.")
                    return existing
            except Exception:
                pass

    cinemas = get_cinemas()
    if not cinemas:
        print("[discover-co] no roster returned — keeping existing schedule.")
        return _load_schedule(date_mdy)

    age_days = None
    if cache.get("updatedUtc"):
        try:
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(cache["updatedUtc"])).total_seconds() / 86400
        except Exception:
            age_days = None
    cached_slugs = cache.get("dboxCitySlugs") or []
    do_full = bool(full or not cached_slugs or age_days is None or age_days >= CACHE_TTL_DAYS)

    slugs = get_city_slugs(cinemas) if do_full else cached_slugs
    print(f"[discover-co] {'FULL' if do_full else 'cached'} scan: {len(slugs)} city slugs on {date_mdy}")

    want_iso = _date_iso(date_mdy)
    showings, dbox_slugs = [], set()
    for i, slug in enumerate(slugs):
        for mv in get_city_movies(slug):
            rows = get_dbox_sessions(slug, mv, want_iso)
            if rows:
                dbox_slugs.add(slug)
            for r in rows:
                # tag each cinema with its display name from the roster
                meta = cinemas.get(r["cinemaId"]) or {}
                r["theatre"] = r.get("theatre") or meta.get("theatre")
                r["city"] = meta.get("city") or r.get("city")
                showings.append(r)
        if do_full and (i + 1) % 5 == 0:
            print(f"  ...scanned {i + 1}/{len(slugs)} cities, {len(showings)} D-BOX showings so far")

    existing = _load_schedule(date_mdy)
    if not showings or (existing and not do_full and len(showings) < 0.5 * len(existing)):
        print(f"[discover-co] scan got {len(showings)} showings"
              + (f" vs {len(existing)} existing" if existing else "")
              + " — hiccup; keeping existing.")
        return existing

    _save_schedule(date_mdy, showings)
    if do_full and dbox_slugs:
        _save_cache(dbox_slugs, cinemas)
        print(f"[discover-co] cached {len(dbox_slugs)} D-BOX city slugs -> {_cache_path()}")
    print(f"[discover-co] {len(showings)} D-BOX sessions across "
          f"{len({s['cinemaId'] for s in showings})} cinemas, "
          f"{len({s['movieId'] for s in showings})} titles -> {_sched_path(date_mdy)}")
    return showings


# ---- measure (2 reads/showing) ---------------------------------------------
def measure_window(date_mdy, grace_min=20):
    sched = _load_schedule(date_mdy)
    if not sched:
        print("[measure-co] no schedule yet — discovering first.")
        sched = discover(date_mdy)
    prev = _load_schedule(_prev_day_mdy(date_mdy))
    if prev:
        seen = {(s.get("cinemaId"), s.get("sessionId")) for s in sched}
        sched = sched + [s for s in prev if (s.get("cinemaId"), s.get("sessionId")) not in seen]

    now = datetime.now(timezone.utc)
    store = {}
    try:
        store = (json.load(open(_data_path())).get("showingsStore") or {})
    except Exception:
        store = {}

    due, next_actions, skipped = [], [], 0
    for s in sched:
        st = _start_utc(s.get("start"))
        if not st:
            continue
        key = f"{(s.get('start') or '')[:10]}|{s.get('cinemaId')}|{s.get('sessionId')}"
        rec = store.get(key)
        final_done = False
        if rec:
            sold, sell = rec.get("sold", 0), rec.get("sellable", 0)
            if sell > 0 and sold >= sell:
                final_done = True
            seen_iso = rec.get("lastSeenUtc")
            if seen_iso:
                try:
                    if datetime.fromisoformat(seen_iso) >= st + timedelta(minutes=FINAL_READ_AFTER_MIN - 2):
                        final_done = True
                except Exception:
                    pass
        if final_done:
            skipped += 1
            continue
        has_read = bool(rec and rec.get("lastSeenUtc"))
        is_early = not has_read
        target = (st - timedelta(minutes=EARLY_READ_MIN) if is_early
                  else st + timedelta(minutes=FINAL_READ_AFTER_MIN))
        latest = st + timedelta(minutes=FINAL_READ_AFTER_MIN + grace_min)
        if target <= now <= latest:
            due.append((st, s, is_early))
            if is_early:
                ft = st + timedelta(minutes=FINAL_READ_AFTER_MIN)
                next_actions.append(ft if ft > now else now + timedelta(minutes=3))
        elif now < target:
            next_actions.append(target)

    due.sort(key=lambda x: x[0])
    print(f"[measure-co] {len(due)} reads due of {len(sched)} scheduled ({skipped} finalized, skipped)")

    rows = []
    for st, s, is_early in due:
        m = measure_showing(s)
        if not m:
            continue
        rows.append({**s, **m})
        print(f"  {'early' if is_early else 'FINAL':5} {str(s.get('movie'))[:22]:22} "
              f"{(s.get('start') or '')[11:16]} C{s.get('cinemaId')} "
              f"D-BOX {m['sold']:2}/{m['sellable']:2} ({m['sell_through']:.0%})  "
              f"reg {m['regular']['sold']}/{m['regular']['sellable']}")

    _write_dashboard_data(rows, scrape_date=date_mdy)

    LO, CAP = 3 * 60, 90 * 60
    wait = (min(next_actions) - now).total_seconds() if next_actions else 75 * 60
    wait = int(min(max(wait, LO), CAP)) + random.randint(0, 45)
    print(f"MEASURED={len(rows)}")
    print(f"NEXT_WAIT_S={wait}")
    return rows


# ---- dashboard data (writes cinemark_co_data.json; same shape as US/BR) -----
def _data_path():
    return _here("dashboard", "cinemark_co_data.json")


def _canon(name):
    return name or ""


def _write_dashboard_data(rows, scrape_date=None, quiet=False):
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
        key = f"{sdate}|{r.get('cinemaId')}|{r.get('sessionId')}"
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
            "date": sdate, "cinemaId": r.get("cinemaId"), "theatre": r.get("theatre"),
            "city": r.get("city"), "movie": r.get("movie"), "movieId": r.get("movieId"),
            "sessionId": r.get("sessionId"), "start": start, "screenTypes": r.get("screenTypes"),
            "sold": sold, "sellable": sellable,
            "regSold": reg.get("sold", (prev or {}).get("regSold", 0)),
            "regSellable": reg.get("sellable", (prev or {}).get("regSellable", 0)),
            "firstSeenUtc": (prev or {}).get("firstSeenUtc", now_iso), "lastSeenUtc": now_iso,
        }

    for rec in store.values():
        if not rec.get("startUtc"):
            su = _start_utc(rec.get("start"))
            rec["startUtc"] = su.isoformat() if su else None
        if not rec.get("cinemarkUrl") and rec.get("sessionId"):
            rec["cinemarkUrl"] = cinemark_url(None, rec.get("movieId"), rec.get("sessionId"))

    def is_realized(rec):
        su = _start_utc(rec.get("start"))
        return su is not None and su + timedelta(minutes=REALIZE_MIN) <= now_utc

    finals, upcoming = [], []
    for rec in store.values():
        if (rec.get("sellable") or 0) <= 0:
            continue
        (finals if is_realized(rec) else upcoming).append(rec)

    def agg(recs):
        s = sum(x.get("sold", 0) for x in recs); se = sum(x.get("sellable", 0) for x in recs)
        return {"seatsSold": s, "seatsSellable": se,
                "sellThrough": round(s / se, 4) if se else 0.0,
                "showings": len(recs), "theatres": len({x.get("cinemaId") for x in recs})}

    realized = agg(finals)
    cmp_recs = [x for x in finals if (x.get("regSellable") or 0) > 0]
    ds = sum(x.get("sold", 0) for x in cmp_recs); dse = sum(x.get("sellable", 0) for x in cmp_recs)
    rs = sum(x.get("regSold", 0) for x in cmp_recs); rse = sum(x.get("regSellable", 0) for x in cmp_recs)
    comparison = {"showings": len(cmp_recs), "dboxSold": ds, "dboxSellable": dse,
                  "regularSold": rs, "regularSellable": rse,
                  "dboxSellThrough": round(ds / dse, 4) if dse else 0.0,
                  "regularSellThrough": round(rs / rse, 4) if rse else 0.0}

    bym = {}
    for x in finals:
        m = bym.setdefault(_canon(x.get("movie")), {"movie": _canon(x.get("movie")), "sold": 0, "sellable": 0, "showings": 0})
        m["sold"] += x.get("sold", 0); m["sellable"] += x.get("sellable", 0); m["showings"] += 1
    by_movie = sorted(({**m, "sellThrough": round(m["sold"] / m["sellable"], 4) if m["sellable"] else 0.0}
                       for m in bym.values()), key=lambda x: -x["sellThrough"])

    byd = {}
    for x in finals:
        d = byd.setdefault(x["date"], {"date": x["date"], "sold": 0, "sellable": 0, "showings": 0})
        d["sold"] += x.get("sold", 0); d["sellable"] += x.get("sellable", 0); d["showings"] += 1
    by_day = sorted(({**d, "sellThrough": round(d["sold"] / d["sellable"], 4) if d["sellable"] else 0.0}
                     for d in byd.values()), key=lambda x: x["date"])

    def rowsf(recs):
        out = []
        for x in recs:
            row = {"cinemaId": x.get("cinemaId"), "theatre": x.get("theatre"), "city": x.get("city"),
                   "movie": _canon(x.get("movie")), "date": x.get("date"),
                   "start": (x.get("start") or "")[11:16], "startUtc": x.get("startUtc"),
                   "cinemarkUrl": x.get("cinemarkUrl"),
                   "sold": x.get("sold", 0), "sellable": x.get("sellable", 0),
                   "lastSeenUtc": x.get("lastSeenUtc"),
                   "sellThrough": round(x["sold"] / x["sellable"], 4) if x.get("sellable") else 0.0}
            if (x.get("regSellable") or 0) > 0:
                row["regSold"] = x.get("regSold", 0); row["regSellable"] = x.get("regSellable", 0)
                row["regSellThrough"] = round(x["regSold"] / x["regSellable"], 4)
            out.append(row)
        return out

    completed = sorted(finals, key=lambda x: (x.get("start") or ""), reverse=True)[:80]
    recent = sorted(finals + upcoming, key=lambda x: (x.get("lastSeenUtc") or ""), reverse=True)[:24]
    upnext = sorted(upcoming, key=lambda x: (x.get("start") or ""))[:60]

    upcoming_sched = []
    if scrape_date:
        seen_up = set()
        for dmdy in (scrape_date, _next_day_mdy(scrape_date)):
            for s in _load_schedule(dmdy):
                tid, sid = s.get("cinemaId"), s.get("sessionId")
                if (tid, sid) in seen_up:
                    continue
                su = _start_utc(s.get("start"))
                if not su or su + timedelta(minutes=REALIZE_MIN) <= now_utc:
                    continue
                seen_up.add((tid, sid))
                rec = store.get(f"{(s.get('start') or '')[:10]}|{tid}|{sid}") or {}
                upcoming_sched.append({
                    "theatre": s.get("theatre"), "cinemaId": tid, "city": s.get("city"),
                    "movie": _canon(s.get("movie")), "start": s.get("start"), "startUtc": su.isoformat(),
                    "cinemarkUrl": cinemark_url(None, s.get("movieId"), sid),
                    "sold": rec.get("sold"), "sellable": rec.get("sellable"),
                    "lastSeenUtc": rec.get("lastSeenUtc")})
        upcoming_sched.sort(key=lambda x: x["startUtc"])
        upcoming_sched = upcoming_sched[:150]

    out = {
        "updatedUtc": now_iso, "source": "Cinemark Colombia · asientos D-BOX",
        "metric": "realized", "showingsStore": store, "upcoming": upcoming_sched, "byDay": by_day,
        "latest": {"scrapeDate": _date_iso(scrape_date) if scrape_date else now_local.strftime("%Y-%m-%d"),
                   "sellThrough": realized["sellThrough"], "seatsSold": realized["seatsSold"],
                   "seatsSellable": realized["seatsSellable"], "showings": realized["showings"],
                   "theatres": realized["theatres"], "upcoming": agg(upcoming), "comparison": comparison,
                   "byMovie": by_movie, "showingsList": rowsf(completed), "recentFills": rowsf(recent),
                   "upcomingList": rowsf(upnext)}}
    json.dump(out, open(path, "w"), indent=2)
    if quiet:
        return
    print(f"  dashboard data -> {path}")
    print(f"  REALIZED: {realized['seatsSold']}/{realized['seatsSellable']} D-BOX seats sold "
          f"across {realized['showings']} completed sessions ({realized['sellThrough']:.0%})")


# ---- demo ------------------------------------------------------------------
def demo():
    f = _here("fixtures", "cinemark_co", "seatmap_dbox.json")
    print("Colombia seat-map parse (fixture):")
    m = summarize_seatmap(json.load(open(f)))
    print(f"  D-BOX seats: {m['total']}  sellable: {m['sellable']}  SOLD: {m['sold']}  ({m['sell_through']:.0%})")
    r = m["regular"]
    print(f"  rest-of-house: {r['sold']}/{r['sellable']} sold ({r['sell_through']:.0%})")
    print("\nLIVE: discover lists today's D-BOX sessions (SeatTypes contains DBOX),")
    print("measure reads each seat-plan ~10 min after showtime; summed across cinemas")
    print("= chain-wide realized D-BOX sell-through, same model as the US/Brazil tools.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover"); d.add_argument("--date", required=True)
    d.add_argument("--max-age", type=int, default=None); d.add_argument("--full", action="store_true")
    m = sub.add_parser("measure"); m.add_argument("--date", required=True)
    m.add_argument("--grace", type=int, default=20)
    sub.add_parser("demo")
    a = p.parse_args()
    if a.cmd == "discover":
        discover(a.date, max_age_min=a.max_age, full=a.full)
    elif a.cmd == "measure":
        measure_window(a.date, grace_min=a.grace)
    else:
        demo()
