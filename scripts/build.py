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
from lib import EFFORTS, POWER_DURS, encode_polyline, haversine, simplify, trim_privacy  # noqa: E402

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


def compute_power(acts, tracks, today, cfg):
    """Best power for each duration (all time and this year), estimated FTP and W/kg."""
    rides = [a for a in acts if a["cat"] == "ride" and tracks.get(a["id"], {}).get("power")]
    if not rides:
        return None

    def best(pool):
        out = {}
        for label in POWER_DURS:
            top = None
            for a in pool:
                w = tracks[a["id"]]["power"].get(label)
                if w and (top is None or w > top[0]):
                    top = (w, a)
            out[label] = {"watts": top[0], "name": top[1]["name"], "date": top[1]["start"][:10]} if top else None
        return out

    year = [a for a in rides if a["start"][:4] == str(today.year)]
    cutoff = (today - dt.timedelta(days=90)).isoformat()
    recent20 = [tracks[a["id"]]["power"].get("20 min") for a in rides if a["start"][:10] >= cutoff]
    recent20 = [w for w in recent20 if w]
    eftp = round(max(recent20) * 0.95) if recent20 else None

    weight = cfg.get("weight_kg")
    wpath = CACHE / "wellness.json"
    if wpath.exists():
        ws = [r["weight"] for r in sorted(json.loads(wpath.read_text()), key=lambda r: r.get("date") or "")
              if r.get("weight")]
        if ws:
            weight = ws[-1]
    zw = cfg.get("zwift") or {}
    return {"all": best(rides), "year": best(year), "eftp": eftp,
            "wkg": round(eftp / weight, 2) if eftp and weight else None,
            "zwift_cat": zw.get("category")}


def drop_duplicates(acts):
    """The same workout can reach Intervals.icu twice (e.g. Zwift directly and again via Garmin).
    Treat two activities as one if they're the same sport, start within 3 minutes of each other
    and their distances are within 10%. Keeps whichever has more detail (longer moving time)."""
    kept, dupes = [], 0
    for a in acts:  # acts is sorted by start time
        start = parse_start(a)
        match = None
        for k in reversed(kept[-6:]):
            if (start - parse_start(k)).total_seconds() > 180:
                break
            same_dist = abs(a["dist_m"] - k["dist_m"]) <= 0.1 * max(a["dist_m"], k["dist_m"], 1)
            if k["cat"] == a["cat"] and same_dist:
                match = k
                break
        if match is None:
            kept.append(a)
        else:
            dupes += 1
            if a["moving_s"] > match["moving_s"]:
                kept[kept.index(match)] = a
    return kept, dupes


FORM_ZONES = [  # Intervals.icu's form (fitness minus fatigue) bands
    (25, "Transition", "Very fresh, fitness may be slipping"),
    (5, "Fresh", "Rested and ready for a hard day"),
    (-10, "Neutral", "Steady, maintaining"),
    (-30, "Optimal", "Productive training load"),
    (-999, "High risk", "Deep fatigue, back off"),
]


def compute_fitness(acts, today):
    """Fitness (CTL), fatigue (ATL) and form for today plus a 90-day fitness trend.

    Uses Intervals.icu's own numbers when the wellness file is there, otherwise
    works them out from each activity's training load (42 and 7 day averages)."""
    series = {}
    source = "intervals"
    wpath = CACHE / "wellness.json"
    if wpath.exists():
        for w in json.loads(wpath.read_text()):
            if w.get("ctl") is not None and w.get("atl") is not None:
                series[w["date"]] = (w["ctl"], w["atl"])
    if not series:
        source = "estimate"
        loads = {}
        for a in acts:
            if a.get("load"):
                d = a["start"][:10]
                loads[d] = loads.get(d, 0) + a["load"]
        if not loads:
            return None
        day = dt.date.fromisoformat(min(loads))
        ctl = atl = 0.0
        while day <= today:
            load = loads.get(day.isoformat(), 0)
            ctl += (load - ctl) / 42
            atl += (load - atl) / 7
            series[day.isoformat()] = (ctl, atl)
            day += dt.timedelta(days=1)
    dates = sorted(d for d in series if d <= today.isoformat())
    if not dates:
        return None
    ctl, atl = series[dates[-1]]
    form = ctl - atl
    zone = next(z for z in FORM_ZONES if form > z[0])
    week_ago = (today - dt.timedelta(days=7)).isoformat()
    past = [d for d in dates if d <= week_ago]
    ramp = ctl - series[past[-1]][0] if past else None
    trend = [round(series[d][0], 1) for d in dates[-90:]]

    # Health numbers, when the watch shares them: latest value plus change over ~30 days
    health = {}
    if wpath.exists():
        rows = sorted(json.loads(wpath.read_text()), key=lambda r: r.get("date") or "")
        month_ago = (today - dt.timedelta(days=30)).isoformat()
        for key in ("vo2max", "rhr", "hrv", "sleep_s", "weight"):
            vals = [(r["date"], r[key]) for r in rows if r.get(key) not in (None, 0)]
            if not vals:
                continue
            latest = vals[-1][1]
            older = [v for d, v in vals if d <= month_ago]
            health[key] = {"value": round(latest, 1), "date": vals[-1][0],
                           "change": round(latest - older[-1], 1) if older else None}
    return {"ctl": round(ctl), "atl": round(atl), "form": round(form), "zone": zone[1], "zone_note": zone[2],
            "ramp": round(ramp, 1) if ramp is not None else None, "trend": trend, "source": source,
            "health": health}


def training_metrics(last4):
    """Averages over the last 4 weeks, in the units the readiness bars use."""
    runs = [a for a in last4 if a["cat"] == "run"]
    rides = [a for a in last4 if a["cat"] == "ride"]
    on_foot = runs + [a for a in last4 if (a.get("type") or "") in ("Hike", "Walk")]
    return {
        "swims_per_week": sum(1 for a in last4 if a["cat"] == "swim") / 4,
        "rides_per_week": len(rides) / 4,
        "ride_hours_per_week": sum(a["moving_s"] for a in rides) / 3600 / 4,
        "long_ride_km": max((a["dist_m"] for a in rides), default=0) / 1000,
        "run_km_per_week": sum(a["dist_m"] for a in runs) / 1000 / 4,
        "run_hours_per_week": sum(a["moving_s"] for a in runs) / 3600 / 4,
        "long_run_km": max((a["dist_m"] for a in runs), default=0) / 1000,
        "climb_per_week": sum(a["elev_m"] for a in on_foot) / 4,
    }


UNITS = {"swims_per_week": "", "rides_per_week": "", "ride_hours_per_week": "h", "long_ride_km": "km",
         "run_km_per_week": "km", "run_hours_per_week": "h", "long_run_km": "km", "climb_per_week": "m"}


def light_for(ratio):
    """Traffic light from how close you are to where you should be by now."""
    if ratio >= 0.85:
        return "green"
    if ratio >= 0.6:
        return "yellow"
    return "red"


def expected_share(weeks_out):
    """How much of the race-day target you'd typically be at this many weeks out.
    About half at 30+ weeks, rising steadily to full 3 weeks out, then easing for the taper."""
    if weeks_out >= 30:
        return 0.45
    if weeks_out <= 0:
        return 0.6
    if weeks_out < 3:
        return 0.75
    return 0.45 + (30 - weeks_out) / 27 * 0.55


def readiness_for_goals(goals, today, last4):
    m = training_metrics(last4)
    out = []
    for g in goals:
        g = dict(g)
        weeks = (dt.date.fromisoformat(g["date"]) - today).days // 7
        share = expected_share(weeks)
        bars = []
        for r in g.get("readiness", []):
            key = r.get("metric")
            if key not in m or not r.get("target"):
                continue
            cur, target = m[key], float(r["target"])
            expected = target * share
            ratio = cur / expected if expected else 0
            status = light_for(ratio)
            bars.append({"label": r.get("label", key), "unit": UNITS[key], "current": round(cur, 1),
                         "ratio": round(ratio, 2),
                         "target": target, "expected": round(expected, 1), "status": status})
        g["readiness"] = bars
        if bars:
            # overall: average of the bars, each capped so one big strength can't hide a weakness
            score = sum(min(b["ratio"], 1.2) for b in bars) / len(bars)
            g["light"] = light_for(score)
            g["score"] = round(score, 2)
        out.append(g)
    return out


def compute_focus(goals, today, fit, last4, load4):
    """Plain-language training focus for the next race, from weeks out plus recent training."""
    upcoming = sorted((g for g in goals if dt.date.fromisoformat(g["date"]) >= today), key=lambda g: g["date"])
    if not upcoming:
        return None
    g = upcoming[0]
    weeks = (dt.date.fromisoformat(g["date"]) - today).days // 7
    if weeks > 24:
        phase, summary = "Base", "Build the aerobic engine. Mostly easy volume, one quality session a week."
    elif weeks > 12:
        phase, summary = "Build", "Raise volume gradually and add race-pace work."
    elif weeks > 2:
        phase, summary = "Race-specific", "Train like the course. Long sessions at race effort."
    else:
        phase, summary = "Taper", "Cut volume, keep a little intensity, sleep a lot."

    def kind(x):
        return (x.get("type", "") + " " + x.get("name", "")).lower()

    def trailish(x):
        return any(k in kind(x) for k in ("trail", "ultra", "mile", "50k", "100k"))

    is_tri = "tri" in kind(g)
    # A trail race within a year shapes training too, even when a triathlon comes first.
    trail_goal = next((x for x in upcoming if trailish(x)
                       and (dt.date.fromisoformat(x["date"]) - today).days <= 365), None)
    is_trail = trail_goal is not None
    trail_name = trail_goal["name"] if trail_goal else g["name"]
    tips = []

    if fit:
        if fit["form"] < -30:
            tips.append(f"Form is {fit['form']}, deep in the red. Take two easy days before the next hard session.")
        elif fit["form"] > 15 and phase != "Taper":
            tips.append(f"You're fresh (form +{fit['form']}). Good day for a key workout.")
        if fit.get("ramp") is not None and fit["ramp"] > 7:
            tips.append(f"Fitness jumped {fit['ramp']:+.0f} in a week. Hold the load steady for a few days.")

    swims = sum(1 for a in last4 if a["cat"] == "swim")
    rides = sum(1 for a in last4 if a["cat"] == "ride")
    if is_tri:
        if swims < 4:
            tips.append(f"{swims} swim{'s' if swims != 1 else ''} in 4 weeks. Aim for 2 a week to be ready for {g['name']}.")
        if rides < 4:
            tips.append(f"{rides} ride{'s' if rides != 1 else ''} in 4 weeks. Add a weekly ride to keep bike fitness.")
    if is_trail or not is_tri:
        long_target = {"Base": 25, "Build": 32, "Race-specific": 40, "Taper": 20}[phase]
        if load4["long_run_km"] < long_target * 0.8:
            tips.append(f"Longest run lately is {load4['long_run_km']} km. Work toward {long_target} km"
                        f"{' for ' + trail_name if is_trail else ''}.")
        if is_trail and load4["elev_m"] < 2000:
            tips.append(f"{load4['elev_m']:,} m of climbing in 4 weeks. Add hills or stairs for {trail_name}.")
    if load4["hours"] < 16:  # under ~4 h a week
        tips.append(f"{load4['hours']} h over 4 weeks. Consistency first: 5 to 7 h a week is a solid {phase.lower()} target.")

    then = [{"name": x["name"], "weeks": (dt.date.fromisoformat(x["date"]) - today).days // 7} for x in upcoming[1:3]]
    return {"race": g["name"], "date": g["date"], "confirmed": g.get("date_confirmed", False), "weeks": weeks,
            "phase": phase, "summary": summary, "tips": tips[:3], "then": then}


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
    acts, dupes = drop_duplicates(acts)
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
        for label, target in EFFORTS.items():
            best = None
            for a in pool:
                t = tracks.get(a["id"], {}).get("efforts", {}).get(label)
                # Activities cached before a distance was added have no effort for it.
                # Fall back to the whole run's moving time, scaled to the distance.
                if not t and a["cat"] == "run" and a["dist_m"] >= target * 0.985 and a["moving_s"]:
                    t = a["moving_s"] * target / a["dist_m"]
                if t and (best is None or t < best[0]):
                    best = (t, a)
            out.append(pb_entry(label, best[0], best[1], "time") if best else {"label": label, "value": None, "unit": "time"})
        for label, unit, cats, key in (("Longest run", "m", ("run",), "dist_m"),
                                       ("Biggest climb", "elev", None, "elev_m"),
                                       ("Longest ride", "m", ("ride",), "dist_m"),
                                       ("Longest swim", "swim", ("swim",), "dist_m")):
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
    power = compute_power(acts, tracks, today, cfg)
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

    goals_out = readiness_for_goals(cfg.get("goals", []), today, last4)
    fitness = compute_fitness(acts, today)
    focus = compute_focus(cfg.get("goals", []), today, fitness, last4, recent_load)

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
        "goals": goals_out,
        "pbs": pbs,
        "pbs_year": pbs_year,
        "power": power,
        "weekly": weekly,
        "yoy": yoy,
        "streaks": streaks,
        "last4": recent_load,
        "fitness": fitness,
        "focus": focus,
        "weather": cfg.get("weather"),
        "recent": recent,
        "heat": {"center": center, "radius_km": radius_km, "label": map_cfg.get("label"), "routes": heat},
        "count_all": len(acts),
    }

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "data").mkdir(parents=True)
    (OUT / "data/stats.json").write_text(json.dumps(stats, separators=(",", ":")))

    # site/*.html are page fragments (head bits, then <!--body-->, then content).
    for src, dst in (("app.html", "index.html"), ("wall.html", "wall.html")):
        frag_path = ROOT / "site" / src
        if not frag_path.exists():
            continue
        head, _, body = frag_path.read_text().partition("<!--body-->")
        (OUT / dst).write_text(
            '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            '<meta name="robots" content="noindex">\n' + head + "</head>\n<body>\n" + body + "</body>\n</html>\n")
    (OUT / ".nojekyll").write_text("")
    size_kb = (OUT / "data/stats.json").stat().st_size // 1024
    print(f"Built site: {len(acts)} activities, {len(heat)} heatmap routes, stats.json {size_kb} KB, "
          f"{len(zones)} privacy zone(s), {dupes} duplicate(s) skipped.")


if __name__ == "__main__":
    main()
