"""Turn data/cache/ into the published site in public/.

Reads:   config.json, data/cache/activities.json, data/cache/tracks/*.json
         privacy zones from the PRIVACY_ZONES env var (a GitHub secret) or privacy.local.json
Writes:  public/index.html, public/data/stats.json
"""
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import statistics
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from lib import EFFORTS, encode_polyline, haversine, simplify, trim_privacy  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = pathlib.Path(os.environ.get("CACHE_DIR", ROOT / "data/cache"))
OUT = pathlib.Path(os.environ.get("OUT_DIR", ROOT / "public"))


def load_privacy():
    raw = os.environ.get("PRIVACY_ZONES", "").strip()
    if not raw and (ROOT / "privacy.local.json").exists():
        raw = (ROOT / "privacy.local.json").read_text()
    if not raw:
        return []
    try:
        zones = json.loads(raw)
        if isinstance(zones, dict):
            zones = [zones]
        if isinstance(zones, list) and all(isinstance(z, dict) and "lat" in z and "lng" in z for z in zones):
            return zones
    except json.JSONDecodeError:
        pass
    # Also accept coordinates pasted straight from Google Maps, e.g. "49.1012, -122.6034",
    # one place per line (or separated by semicolons). Optional third number = radius in metres.
    zones = []
    for part in re.split(r"[;\n]+", raw):
        nums = re.findall(r"-?\d+(?:\.\d+)?", part)
        if len(nums) >= 2:
            lat, lng = float(nums[0]), float(nums[1])
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                zones.append({"lat": lat, "lng": lng, "radius_m": float(nums[2]) if len(nums) > 2 else 500})
    if not zones:
        # never print the secret itself, the logs of a public repo are public
        sys.exit("PRIVACY_ZONES is set but couldn't be read. Paste coordinates like 49.1012, -122.6034 "
                 "(one place per line), or use the JSON format in privacy.example.json.")
    return zones


def parse_start(a):
    return dt.datetime.fromisoformat(a["start"].replace("Z", ""))


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    tz = ZoneInfo(cfg.get("timezone", "America/Vancouver"))
    today = dt.date.fromisoformat(os.environ["TODAY"]) if os.environ.get("TODAY") else dt.datetime.now(tz).date()
    meta = json.loads((CACHE / "meta.json").read_text()) if (CACHE / "meta.json").exists() else {}
    sample = bool(meta.get("sample"))

    zones = load_privacy()
    if not zones and not sample and not os.environ.get("ALLOW_NO_PRIVACY"):
        sys.exit("No privacy zones set. Add the PRIVACY_ZONES secret (see README), "
                 "or set ALLOW_NO_PRIVACY=1 if you really want full routes public.")

    acts = list(json.loads((CACHE / "activities.json").read_text()).values())
    acts = [a for a in acts if a.get("start")]
    acts.sort(key=parse_start)
    tracks = {}
    for a in acts:
        p = CACHE / "tracks" / f"{a['id']}.json"
        if p.exists():
            tracks[a["id"]] = json.loads(p.read_text())

    year = today.year
    this_year = [a for a in acts if parse_start(a).year == year]

    # ---- season totals
    totals = {
        "year": year,
        "km": round(sum(a["dist_m"] for a in this_year) / 1000),
        "elev_m": round(sum(a["elev_m"] for a in this_year)),
        "hours": round(sum(a["moving_s"] for a in this_year) / 3600),
        "count": len(this_year),
    }

    # ---- personal bests
    def pb_entry(label, value, a, unit):
        return {"label": label, "value": value, "unit": unit, "name": a["name"],
                "date": a["start"][:10], "new": parse_start(a).year == year}

    def compute_pbs(pool):
        """Same fixed list of slots for every pool, so the all-time and this-year rows line up.
        A slot with nothing yet comes back with value None."""
        out = []
        for label in EFFORTS:
            best = None
            for a in pool:
                t = tracks.get(a["id"], {}).get("efforts", {}).get(label)
                if t and (best is None or t < best[0]):
                    best = (t, a)
            out.append(pb_entry(label, best[0], best[1], "time") if best else {"label": label, "value": None, "unit": "time"})
        for label, unit, cats, key in (("Longest run", "m", ("run",), "dist_m"),
                                       ("Biggest climb", "elev", None, "elev_m"),
                                       ("Longest ride", "m", ("ride",), "dist_m")):
            cands = [a for a in pool if (cats is None or a["cat"] in cats) and a[key]]
            if cands:
                a = max(cands, key=lambda x: x[key])
                out.append(pb_entry(label, a[key], a, unit))
            else:
                out.append({"label": label, "value": None, "unit": unit})
        return out

    runs = [a for a in acts if a["cat"] == "run"]
    rides = [a for a in acts if a["cat"] == "ride"]
    pbs = compute_pbs(acts)
    pbs_year = compute_pbs(this_year)
    # flag this-year bests that are also the all-time best
    for y, a in zip(pbs_year, pbs):
        y["is_pb"] = y["value"] is not None and y["value"] == a["value"] and y.get("date") == a.get("date")

    # ---- weekly hours by sport, last 16 weeks (Mon-Sun)
    week0 = today - dt.timedelta(days=today.weekday()) - dt.timedelta(weeks=15)
    weekly = [{"start": (week0 + dt.timedelta(weeks=i)).isoformat(), "run": 0.0, "ride": 0.0, "swim": 0.0, "other": 0.0}
              for i in range(16)]
    for a in acts:
        d = parse_start(a).date()
        i = (d - week0).days // 7
        if 0 <= i < 16:
            weekly[i][a["cat"]] += a["moving_s"] / 3600
    for w in weekly:
        for k in ("run", "ride", "swim", "other"):
            w[k] = round(w[k], 2)

    # ---- year over year cumulative running km
    def cumulative(y, upto_month):
        months = [0.0] * 12
        for a in runs:
            s = parse_start(a)
            if s.year == y:
                months[s.month - 1] += a["dist_m"] / 1000
        out, acc = [], 0.0
        for m in range(upto_month):
            acc += months[m]
            out.append(round(acc))
        return out

    yoy = {"this_year": year, "last_year": year - 1,
           "this": cumulative(year, today.month), "last": cumulative(year - 1, 12)}

    # ---- streaks and comparisons
    run_days = {parse_start(a).date() for a in runs}
    streak, d = 0, today if today in run_days else today - dt.timedelta(days=1)
    while d in run_days:
        streak += 1
        d -= dt.timedelta(days=1)
    big_weeks = sum(1 for w in weekly if w["run"] + w["ride"] + w["swim"] + w["other"] >= 8)
    doy = today.timetuple().tm_yday
    km_this = sum(a["dist_m"] for a in runs if parse_start(a).year == year) / 1000
    km_last = sum(a["dist_m"] for a in runs
                  if parse_start(a).year == year - 1 and parse_start(a).timetuple().tm_yday <= doy) / 1000
    streaks = {"run_streak": streak, "big_weeks": big_weeks,
               "vs_last_pct": round((km_this / km_last - 1) * 100) if km_last else None}

    # ---- last 4 weeks, shown under the race countdowns
    cutoff = today - dt.timedelta(days=28)
    last4 = [a for a in acts if parse_start(a).date() > cutoff]
    last4_runs = [a for a in last4 if a["cat"] == "run"]
    recent_load = {
        "hours": round(sum(a["moving_s"] for a in last4) / 3600, 1),
        "long_run_km": round(max((a["dist_m"] for a in last4_runs), default=0) / 1000, 1),
        "elev_m": round(sum(a["elev_m"] for a in last4)),
        "count": len(last4),
    }

    # ---- privacy-trimmed routes
    def public_route(aid, eps):
        pts = tracks.get(aid, {}).get("route") or []
        segs = trim_privacy([tuple(p) for p in pts], zones)
        return [encode_polyline(simplify(s, eps)) for s in segs if len(s) > 1]

    # Heatmap window: centre on where most activities start, so trips abroad don't shrink the home map.
    starts = [tracks[a["id"]]["route"][0] for a in acts if tracks.get(a["id"], {}).get("route")]
    map_cfg = cfg.get("map", {})
    radius_km = map_cfg.get("radius_km", 35)
    if map_cfg.get("center"):
        center = map_cfg["center"]
    elif starts:
        center = [statistics.median(p[0] for p in starts), statistics.median(p[1] for p in starts)]
    else:
        center = [49.1, -122.6]
    center = [round(center[0], 2), round(center[1], 2)]  # coarse on purpose
    heat = []
    for a in acts:
        if a["cat"] not in ("run", "ride"):
            continue
        r = tracks.get(a["id"], {}).get("route") or []
        if not r or min(haversine(p, center) for p in r[:: max(1, len(r) // 20)]) > radius_km * 1000:
            continue
        segs = public_route(a["id"], 12)
        if segs:
            heat.append({"c": a["cat"], "y": parse_start(a).year, "p": segs})

    # ---- recent activities
    recent = []
    for a in reversed(acts[-6:]):
        recent.append({k: a[k] for k in ("name", "type", "cat", "start", "dist_m", "moving_s", "elev_m", "hr")}
                      | {"route": public_route(a["id"], 25)})

    stats = {
        "generated": dt.datetime.now(tz).isoformat(timespec="minutes"),
        "today": today.isoformat(),
        "sample": sample,
        "athlete": cfg.get("athlete"),
        "title": cfg.get("title"),
        "location": cfg.get("location"),
        "sports_line": cfg.get("sports_line"),
        "totals": totals,
        "goals": cfg.get("goals", []),
        "pbs": pbs,
        "pbs_year": pbs_year,
        "weekly": weekly,
        "yoy": yoy,
        "streaks": streaks,
        "last4": recent_load,
        "recent": recent,
        "heat": {"center": center, "radius_km": radius_km, "label": map_cfg.get("label"), "routes": heat},
        "count_all": len(acts),
    }

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "data").mkdir(parents=True)
    (OUT / "data/stats.json").write_text(json.dumps(stats, separators=(",", ":")))

    # site/app.html is a page fragment (head bits, then <!--body-->, then content).
    frag = (ROOT / "site/app.html").read_text()
    head, _, body = frag.partition("<!--body-->")
    (OUT / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="robots" content="noindex">\n' + head + "</head>\n<body>\n" + body + "</body>\n</html>\n")
    (OUT / ".nojekyll").write_text("")
    size_kb = (OUT / "data/stats.json").stat().st_size // 1024
    print(f"Built site: {len(acts)} activities, {len(heat)} heatmap routes, stats.json {size_kb} KB, "
          f"{len(zones)} privacy zone(s).")


if __name__ == "__main__":
    main()
