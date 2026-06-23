#!/usr/bin/env python3
"""
Cinemark BRAZIL D-BOX scraper — realized D-BOX sell-through, same metric as the
US tracker but against Brazil's platform.

Brazil (cinemark.com.br) is a client-rendered app backed by a clean JSON BFF API
(NOT the US server-rendered HTML), so this scraper talks to that API directly:

  base: https://br-www-frontend-ext-prod.cinemark.com.br/bff-api/v1
  1. states?hasCinemark=true                 -> states with Cinemark
  2. cities?hasCinemark=true&stateId=<id>     -> cities per state
  3. theaters?cityId=<id>                     -> theatres (code, name, city, state,
                                                sessionTypes[]).  Keep those whose
                                                sessionTypes includes "DBOX".
  4. sessions/movie?theaterId=<code>          -> movies -> dates -> rooms -> sessions
                                                (a D-BOX room carries feature code 6)
  5. seatmaps?theaterId=<code>&sessionId=<id> -> elements[] (every seat). D-BOX seats
                                                are type 12 ("D-Box"); status 3 = sold,
                                                2 = blocked, 1 = available, 22 = selected.

D-BOX sell-through = (type-12 seats with status 3) / (type-12 seats, excl. blocked),
with NORMAL/VIP seats as the rest-of-house comparison. Same realized model as the US
tracker: two reads per showing (early + ~10 min after showtime), gzip, polite delays.

    python cinemark_br_scraper.py discover --date 6/23/2026
    python cinemark_br_scraper.py measure  --date 6/23/2026
    python cinemark_br_scraper.py demo      # offline, bundled fixture
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

SITE = "https://www.cinemark.com.br"
API = "https://br-www-frontend-ext-prod.cinemark.com.br/bff-api/v1"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-encoding": "gzip",
    "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
    "origin": SITE,
    "referer": SITE + "/",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"),
}
DELAY_RANGE_S = (0.8, 2.2)
REALIZE_MIN = 15

# Seat model (from the seatmaps legend):
DBOX_TYPE = 12                 # "D-Box"
REGULAR_TYPES = {1, 4, 13, 14}  # NORMAL, VIP, love-seat L/R = the rest of the house
ST_AVAILABLE, ST_BLOCKED, ST_SOLD, ST_SELECTED = 1, 2, 3, 22
DBOX_ROOM_FEATURE = 6          # a D-BOX room carries this feature code in sessions/movie

# Two reads per showing (cost control), same as US.
EARLY_READ_MIN = 45
FINAL_READ_AFTER_MIN = 10

# ---- proxy + session -------------------------------------------------------
PROXY = (os.environ.get("CINEMARK_BR_PROXY") or os.environ.get("HTTPS_PROXY")
         or os.environ.get("https_proxy") or "").strip() or None


def _build_opener():
    handlers = [urllib.request.HTTPCookieProcessor(CookieJar())]
    handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY} if PROXY else {}))
    return urllib.request.build_opener(*handlers)


_OPENER = _build_opener()
_WARNED = False


def _get_json(path, tries=3):
    """GET API path -> parsed JSON (decompressing gzip), or None."""
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
            if e.code in (403, 429) and not _WARNED:
                _WARNED = True
                print(f"  [warn] HTTP {e.code} from Cinemark BR — may need a BR residential "
                      f"proxy (set CINEMARK_BR_PROXY). Degrading gracefully.")
            return None
        except (ValueError, urllib.error.URLError, OSError):
            if attempt < tries - 1:
                time.sleep(1.0 * (attempt + 1)); continue
            return None
    return None


def _data(j):
    """Unwrap the BFF envelope: {success, messageError, dataResult}."""
    if isinstance(j, dict) and "dataResult" in j:
        return j.get("dataResult")
    return j


# ---- timezone (Brazil state name -> IANA) ----------------------------------
# Most of Cinemark's Brazil footprint is UTC-3 (America/Sao_Paulo, no DST). A few
# western/northern states differ.
_BR_TZ = {
    "amazonas": "America/Manaus", "roraima": "America/Boa_Vista",
    "rondonia": "America/Porto_Velho", "mato grosso": "America/Cuiaba",
    "mato grosso do sul": "America/Campo_Grande", "acre": "America/Rio_Branco",
}
_BR_DEFAULT_TZ = "America/Sao_Paulo"


def _norm(s):
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").strip().lower())
                   if unicodedata.category(c) != "Mn")


def _tz_for(state):
    if zoneinfo is None:
        return timezone(timedelta(hours=-3))
    try:
        return zoneinfo.ZoneInfo(_BR_TZ.get(_norm(state), _BR_DEFAULT_TZ))
    except Exception:
        return zoneinfo.ZoneInfo(_BR_DEFAULT_TZ)


def _start_utc(start_str, state=None):
    try:
        naive = datetime.strptime((start_str or "")[:16], "%Y-%m-%dT%H:%M")
    except Exception:
        return None
    try:
        return naive.replace(tzinfo=_tz_for(state)).astimezone(timezone.utc)
    except Exception:
        return None


def _here(*p):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *p)


# ---- 1. roster: all Brazil theatres that have D-BOX ------------------------
def get_dbox_theatres():
    """states -> cities -> theaters; keep theatres whose sessionTypes includes DBOX.
    Returns [{code, theatre, city, state}]."""
    states = _data(_get_json("/states?hasCinemark=true")) or []
    out, seen = [], set()
    for stt in states:
        sid = stt.get("id")
        cities = _data(_get_json(f"/cities?hasCinemark=true&stateId={sid}")) or []
        for c in cities:
            cid = c.get("id")
            theaters = _data(_get_json(f"/theaters?cityId={cid}")) or []
            for t in theaters:
                code = t.get("code")
                if code is None or code in seen:
                    continue
                stypes = [str(x).upper() for x in (t.get("sessionTypes") or [])]
                if "DBOX" not in stypes:
                    continue
                seen.add(code)
                out.append({"code": code, "theatre": t.get("name"),
                            "city": t.get("city"), "state": t.get("state")})
    return out


# ---- 2. discover: today's D-BOX sessions -----------------------------------
def parse_dbox_sessions(sessions_json, theatre=None, state=None, want_iso=None):
    """From a theatre's sessions/movie payload, return D-BOX sessions (rooms whose
    features include the D-BOX code), optionally filtered to want_iso date."""
    dr = _data(sessions_json) or {}
    out, seen = [], set()
    for m in dr.get("movies") or []:
        mv = m.get("movie") or {}
        title = mv.get("title") or mv.get("name") or mv.get("originalTitle")
        slug = mv.get("slug") or mv.get("urlKey") or _slugify(title)
        for d in m.get("dates") or []:
            for rm in d.get("rooms") or []:
                if DBOX_ROOM_FEATURE not in (rm.get("features") or []):
                    continue  # not a D-BOX room
                for s in rm.get("sessions") or []:
                    sid = s.get("id")
                    start = s.get("date")
                    if not sid or sid in seen:
                        continue
                    if want_iso and (start or "")[:10] != want_iso:
                        continue
                    if s.get("expired"):
                        continue
                    seen.add(sid)
                    out.append({"theatreId": None, "theatre": theatre, "state": state,
                                "movie": title, "movieSlug": slug,
                                "sessionId": sid, "start": (start or "")[:19]})
    return out


def _slugify(name):
    s = _norm(name)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "filme"


def get_dbox_sessions(theatre, want_iso):
    js = _get_json(f"/sessions/movie?theaterId={theatre['code']}")
    rows = parse_dbox_sessions(js, theatre=theatre["theatre"], state=theatre["state"],
                               want_iso=want_iso)
    for r in rows:
        r["theatreId"] = theatre["code"]
    return rows


# ---- 3. measure: seat map -> D-BOX vs rest-of-house ------------------------
def summarize_seatmap(seatmap_json):
    """Tally D-BOX (type 12) vs rest-of-house from a seatmaps payload.
    sold = status 3 (Ocupado); blocked (status 2) is not sellable. Returns D-BOX
    stats with 'regular' nested, or None if no elements."""
    dr = _data(seatmap_json) or {}
    els = dr.get("elements")
    if not els:
        return None

    def tally(types):
        total = sold = 0
        for e in els:
            if e.get("type") not in types:
                continue
            stt = e.get("status")
            if stt == ST_BLOCKED:
                continue  # not sellable
            total += 1
            if stt == ST_SOLD:
                sold += 1
        return {"total": total, "sellable": total, "sold": sold,
                "available": total - sold,
                "sell_through": sold / total if total else 0.0}

    out = tally({DBOX_TYPE})
    out["regular"] = tally(REGULAR_TYPES)
    return out


def seatmap_url(theatre_id, session_id):
    return f"{API}/seatmaps?theaterId={theatre_id}&sessionId={session_id}"


def cinemark_url(slug, session_id):
    return f"{SITE}/filme/{slug or 'filme'}/assentos?sessionId={session_id}"


def measure_showing(s):
    js = _get_json(seatmap_url(s["theatreId"], s["sessionId"]))
    if not js:
        return None
    m = summarize_seatmap(js)
    if not m or m["total"] <= 0:
        return None  # no D-BOX seats found — skip
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
    return _here("schedule_cinemark_br", f"{_date_iso(mdy)}.json")


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
    return _here("dbox_theatres_cache_br.json")


def _load_cache():
    try:
        return json.load(open(_cache_path()))
    except Exception:
        return {}


def _save_cache(theatres):
    json.dump({"updatedUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "count": len(theatres), "theatres": theatres}, open(_cache_path(), "w"), indent=2)


CACHE_TTL_DAYS = 7


# ---- discover --------------------------------------------------------------
def discover(date_mdy, max_age_min=None, full=False):
    if max_age_min is not None:
        p = _sched_path(date_mdy)
        if os.path.exists(p):
            try:
                built = json.load(open(p)).get("builtUtc")
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(built)).total_seconds() / 60
                existing = _load_schedule(date_mdy)
                if age < max_age_min and len(existing) >= 5:
                    print(f"[discover-br] schedule {age:.0f} min old with {len(existing)} showings — skipping.")
                    return existing
            except Exception:
                pass

    cache = _load_cache()
    age_days = None
    if cache.get("updatedUtc"):
        try:
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(cache["updatedUtc"])).total_seconds() / 86400
        except Exception:
            age_days = None
    if full or not cache.get("theatres") or age_days is None or age_days >= CACHE_TTL_DAYS:
        theatres = get_dbox_theatres()
        if theatres:
            _save_cache(theatres)
        print(f"[discover-br] full roster scan: {len(theatres)} D-BOX theatres in Brazil")
    else:
        theatres = cache.get("theatres", [])
        print(f"[discover-br] cached roster: {len(theatres)} D-BOX theatres (cache {age_days:.1f}d old)")

    want_iso = _date_iso(date_mdy)
    showings = []
    for t in theatres:
        showings += get_dbox_sessions(t, want_iso)

    existing = _load_schedule(date_mdy)
    if not showings or (existing and len(showings) < 0.5 * len(existing)):
        print(f"[discover-br] scan got {len(showings)} showings"
              + (f" vs {len(existing)} existing" if existing else "")
              + " — hiccup; keeping existing.")
        return existing
    _save_schedule(date_mdy, showings)
    print(f"[discover-br] {len(showings)} D-BOX sessions across "
          f"{len({s['theatreId'] for s in showings})} theatres, "
          f"{len({s['movie'] for s in showings})} titles -> {_sched_path(date_mdy)}")
    return showings


# ---- measure (2 reads/showing) ---------------------------------------------
def measure_window(date_mdy, grace_min=20):
    sched = _load_schedule(date_mdy)
    if not sched:
        print("[measure-br] no schedule yet — discovering first.")
        sched = discover(date_mdy)
    prev = _load_schedule(_prev_day_mdy(date_mdy))
    if prev:
        seen = {(s.get("theatreId"), s.get("sessionId")) for s in sched}
        sched = sched + [s for s in prev if (s.get("theatreId"), s.get("sessionId")) not in seen]

    now = datetime.now(timezone.utc)
    store = {}
    try:
        store = (json.load(open(_data_path())).get("showingsStore") or {})
    except Exception:
        store = {}

    due, next_actions, skipped = [], [], 0
    for s in sched:
        st = _start_utc(s.get("start"), s.get("state"))
        if not st:
            continue
        key = f"{(s.get('start') or '')[:10]}|{s.get('theatreId')}|{s.get('sessionId')}"
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
    print(f"[measure-br] {len(due)} reads due of {len(sched)} scheduled ({skipped} finalized, skipped)")

    rows = []
    for st, s, is_early in due:
        m = measure_showing(s)
        if not m:
            continue
        rows.append({**s, **m})
        print(f"  {'early' if is_early else 'FINAL':5} {str(s.get('movie'))[:22]:22} "
              f"{(s.get('start') or '')[11:16]} T{s.get('theatreId')} "
              f"D-BOX {m['sold']:2}/{m['sellable']:2} ({m['sell_through']:.0%})  "
              f"reg {m['regular']['sold']}/{m['regular']['sellable']}")

    _write_dashboard_data(rows, scrape_date=date_mdy)

    LO, CAP = 3 * 60, 90 * 60
    wait = (min(next_actions) - now).total_seconds() if next_actions else 75 * 60
    wait = int(min(max(wait, LO), CAP)) + random.randint(0, 45)
    print(f"MEASURED={len(rows)}")
    print(f"NEXT_WAIT_S={wait}")
    return rows


# ---- dashboard data (writes cinemark_br_data.json; same shape as US) --------
def _data_path():
    return _here("dashboard", "cinemark_br_data.json")


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
        key = f"{sdate}|{r.get('theatreId')}|{r.get('sessionId')}"
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
            "date": sdate, "theatreId": r.get("theatreId"), "theatre": r.get("theatre"),
            "movie": r.get("movie"), "showtimeId": r.get("sessionId"),
            "start": start, "state": r.get("state"), "movieSlug": r.get("movieSlug"),
            "sold": sold, "sellable": sellable,
            "regSold": reg.get("sold", (prev or {}).get("regSold", 0)),
            "regSellable": reg.get("sellable", (prev or {}).get("regSellable", 0)),
            "firstSeenUtc": (prev or {}).get("firstSeenUtc", now_iso), "lastSeenUtc": now_iso,
        }

    for rec in store.values():
        if not rec.get("startUtc"):
            su = _start_utc(rec.get("start"), rec.get("state"))
            rec["startUtc"] = su.isoformat() if su else None
        if not rec.get("cinemarkUrl") and rec.get("showtimeId"):
            rec["cinemarkUrl"] = cinemark_url(rec.get("movieSlug"), rec.get("showtimeId"))

    def is_realized(rec):
        su = _start_utc(rec.get("start"), rec.get("state"))
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
                "showings": len(recs), "theatres": len({x.get("theatreId") for x in recs})}

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
            row = {"theatreId": x.get("theatreId"), "theatre": x.get("theatre"),
                   "movie": _canon(x.get("movie")), "date": x.get("date"),
                   "start": (x.get("start") or "")[11:16], "startUtc": x.get("startUtc"),
                   "state": x.get("state"), "cinemarkUrl": x.get("cinemarkUrl"),
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
                tid, sid = s.get("theatreId"), s.get("sessionId")
                if (tid, sid) in seen_up:
                    continue
                su = _start_utc(s.get("start"), s.get("state"))
                if not su or su + timedelta(minutes=REALIZE_MIN) <= now_utc:
                    continue
                seen_up.add((tid, sid))
                rec = store.get(f"{(s.get('start') or '')[:10]}|{tid}|{sid}") or {}
                upcoming_sched.append({
                    "theatre": s.get("theatre"), "theatreId": tid, "movie": _canon(s.get("movie")),
                    "start": s.get("start"), "startUtc": su.isoformat(),
                    "cinemarkUrl": cinemark_url(s.get("movieSlug"), sid),
                    "sold": rec.get("sold"), "sellable": rec.get("sellable"),
                    "lastSeenUtc": rec.get("lastSeenUtc")})
        upcoming_sched.sort(key=lambda x: x["startUtc"])
        upcoming_sched = upcoming_sched[:150]

    out = {
        "updatedUtc": now_iso, "source": "Cinemark Brasil · assentos D-BOX",
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
    f = _here("fixtures", "cinemark_br", "seatmap_dbox.json")
    print("Brazil seat-map parse (fixture):")
    m = summarize_seatmap(json.load(open(f)))
    print(f"  D-BOX seats: {m['total']}  SOLD: {m['sold']}  ({m['sell_through']:.0%})")
    print(f"  rest-of-house: {m['regular']['sold']}/{m['regular']['sellable']} ({m['regular']['sell_through']:.0%})")


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
