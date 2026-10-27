"""Shared helpers: sport categories, best efforts, route simplification, privacy trimming, polyline encoding."""
import math

EARTH_R = 6371000.0

# Distances (metres) we look for fastest efforts on, for runs.
# Durations (seconds) we look for best average power on, for rides.
POWER_DURS = {"5s": 5, "1 min": 60, "5 min": 300, "20 min": 1200}

EFFORTS = {"5K": 5000, "10K": 10000, "Half": 21097.5, "Marathon": 42195, "50K": 50000, "100K": 100000}


def category(sport_type):
    t = (sport_type or "").lower()
    if "run" in t:
        return "run"
    if "ride" in t or "cycl" in t or "bike" in t:
        return "ride"
    if "swim" in t:
        return "swim"
    return "other"


def haversine(a, b):
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def best_efforts(time, dist, targets=EFFORTS):
    """Fastest time (s) to cover each target distance anywhere inside the activity.

    time and dist are parallel streams (seconds, metres). Uses a sliding window with
    linear interpolation at the far end, so a 5K PB can come from the middle of a 10K.
    """
    pts = [(t, d) for t, d in zip(time or [], dist or []) if t is not None and d is not None]
    if len(pts) < 2:
        return {}
    ts, ds, run_max = [], [], 0.0
    for t, d in pts:
        run_max = max(run_max, d)  # GPS distance can wobble backwards, keep it monotonic
        ts.append(t)
        ds.append(run_max)
    n = len(ts)
    out = {}
    for label, target in targets.items():
        covered = ds[-1] - ds[0]
        if covered < target:
            # GPS often measures a race a little short (a 5K showing 4.97 km).
            # Within 1.5%, count the whole activity, scaled up to the full distance.
            if covered >= target * 0.985 and ts[-1] > ts[0]:
                out[label] = round((ts[-1] - ts[0]) * target / covered, 1)
            continue
        best = math.inf
        j = 0
        for i in range(n):
            j = max(j, i + 1)
            while j < n and ds[j] - ds[i] < target:
                j += 1
            if j >= n:
                break
            seg = ds[j] - ds[j - 1]
            frac = (target - (ds[j - 1] - ds[i])) / seg if seg > 0 else 1.0
            t_end = ts[j - 1] + (ts[j] - ts[j - 1]) * frac
            best = min(best, t_end - ts[i])
        if best < math.inf:
            out[label] = round(best, 1)
    return out


def _to_xy(p, lat0):
    return (math.radians(p[1]) * EARTH_R * math.cos(math.radians(lat0)), math.radians(p[0]) * EARTH_R)


def best_power(time, watts, durs=POWER_DURS):
    """Best average power (W) for each duration. Resamples to one value per second,
    carrying each reading forward; gaps over 5 s (paused) count as zero."""
    pts = [(t, w) for t, w in zip(time or [], watts or []) if t is not None]
    if len(pts) < 2:
        return {}
    t0, t_end = pts[0][0], pts[-1][0]
    n = int(t_end - t0) + 1
    if n < 5 or n > 60 * 60 * 30:
        return {}
    sec = [0.0] * n
    for (t, w), (t_next, _) in zip(pts, pts[1:] + [(t_end + 1, None)]):
        a = int(t - t0)
        b = min(n, int(t_next - t0), a + 5)  # hold a reading at most 5 s
        for i in range(a, b):
            sec[i] = float(w or 0)
    out = {}
    for label, d in durs.items():
        if d > n:
            continue
        run = sum(sec[:d])
        best = run
        for i in range(d, n):
            run += sec[i] - sec[i - d]
            if run > best:
                best = run
        if best > 0:
            out[label] = round(best / d)
    return out


def simplify(points, eps_m=8.0):
    """Ramer-Douglas-Peucker on lat/lng points, tolerance in metres."""
    if len(points) < 3:
        return list(points)
    lat0 = points[0][0]
    xy = [_to_xy(p, lat0) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        s, e = stack.pop()
        (x1, y1), (x2, y2) = xy[s], xy[e]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy)
        far, idx = 0.0, -1
        for k in range(s + 1, e):
            x0, y0 = xy[k]
            d = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / norm if norm else math.hypot(x0 - x1, y0 - y1)
            if d > far:
                far, idx = d, k
        if far > eps_m and idx > 0:
            keep[idx] = True
            stack += [(s, idx), (idx, e)]
    return [p for p, k in zip(points, keep) if k]


def trim_privacy(points, zones):
    """Remove every point inside a privacy zone. Returns a list of segments (lists of points)."""
    if not zones:
        return [points] if len(points) > 1 else []
    segs, cur = [], []
    for p in points:
        hidden = any(haversine(p, (z["lat"], z["lng"])) <= z.get("radius_m", 500) for z in zones)
        if hidden:
            if len(cur) > 1:
                segs.append(cur)
            cur = []
        else:
            cur.append(p)
    if len(cur) > 1:
        segs.append(cur)
    return segs


def encode_polyline(points, precision=5):
    """Google encoded polyline. Keeps the published route data small."""
    factor = 10 ** precision
    out, prev_lat, prev_lng = [], 0, 0
    for lat, lng in points:
        ilat, ilng = round(lat * factor), round(lng * factor)
        for v in (ilat - prev_lat, ilng - prev_lng):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lng = ilat, ilng
    return "".join(out)


def parse_streams(raw):
    """Normalise an Intervals.icu streams response into {'time': [...], 'distance': [...], 'latlng': [(lat,lng)...]}."""
    out = {}
    if isinstance(raw, dict):  # some endpoints return {type: {data: [...]}}
        raw = [dict(v, type=k) if isinstance(v, dict) else {"type": k, "data": v} for k, v in raw.items()]
    for s in raw or []:
        typ, data = s.get("type"), s.get("data") or []
        if typ == "latlng":
            if s.get("data2"):  # Intervals splits lat (data) and lng (data2)
                pts = list(zip(data, s["data2"]))
            else:
                pts = [tuple(p) for p in data if isinstance(p, (list, tuple)) and len(p) == 2]
            out["latlng"] = [(a, b) for a, b in pts if a is not None and b is not None and not (a == 0 and b == 0)]
        elif typ in ("time", "distance", "watts"):
            out[typ] = data
    return out


def process_activity(summary, streams):
    """Turn an API activity + its streams into the two compact records we cache.

    The full streams are thrown away afterwards, which keeps the cache small enough
    to restore in seconds on every nightly run.
    """
    cat = category(summary.get("type"))
    rec = {
        "id": str(summary.get("id")),
        "name": summary.get("name") or summary.get("type") or "Activity",
        "type": summary.get("type"),
        "cat": cat,
        "start": summary.get("start_date_local"),
        "dist_m": summary.get("distance") or summary.get("icu_distance") or 0,
        "moving_s": summary.get("moving_time") or summary.get("elapsed_time") or 0,
        "elev_m": summary.get("total_elevation_gain") or 0,
        "hr": summary.get("average_heartrate"),
        "load": summary.get("icu_training_load"),
        "race": bool(summary.get("race")),
        "watts": summary.get("icu_average_watts") or summary.get("average_watts"),
    }
    track = {"id": rec["id"], "efforts": {}, "route": []}
    if streams:
        if cat == "run":
            track["efforts"] = best_efforts(streams.get("time"), streams.get("distance"))
        if cat == "ride":
            track["power"] = best_power(streams.get("time"), streams.get("watts"))
        pts = streams.get("latlng") or []
        if len(pts) > 1:
            track["route"] = [[round(a, 6), round(b, 6)] for a, b in simplify(pts, 8.0)]
    return rec, track
