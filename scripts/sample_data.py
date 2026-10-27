"""Create a fake but realistic data/cache so the site can be built and previewed without an API key.

Runs the generated streams through the same processing as real activities, so it
also exercises the PB and route code. Everything it makes is flagged as sample data.
"""
import datetime as dt
import json
import math
import os
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from lib import process_activity  # noqa: E402

CACHE = pathlib.Path(os.environ.get("CACHE_DIR", "data/cache"))
rng = random.Random(42)

SPOTS = {  # rough areas only, nothing personal
    "fraser": (49.165, -122.575),
    "campbell": (49.015, -122.66),
    "northshore": (49.345, -123.03),
    "glenvalley": (49.18, -122.50),
}
NAMES = {
    "run": ["Easy miles", "Tempo 3 x 3 km", "Recovery jog", "Hill repeats", "Long run", "Progression run"],
    "trail": ["Lynn Headwaters loop", "Campbell Valley loops", "Seymour grind", "Derby Reach trails", "Grouse BCMC"],
    "ride": ["Glen Valley loop", "Zero Avenue out-and-back", "Fort Langley spin", "Coffee ride"],
}


def track(center, dist_m, speed, step_s=10, wiggle=0.35):
    """Random-walk loop that drifts out and comes home, with time/distance streams."""
    lat, lng = center[0] + rng.uniform(-0.01, 0.01), center[1] + rng.uniform(-0.01, 0.01)
    home = (lat, lng)
    heading = rng.uniform(0, 2 * math.pi)
    t = d = 0.0
    times, dists, pts = [], [], []
    while d < dist_m:
        times.append(round(t))
        dists.append(round(d, 1))
        pts.append((lat, lng))
        v = max(1.0, speed * rng.uniform(0.9, 1.1))
        step = v * step_s
        # turn toward home during the second half so the route closes into a loop
        if d > dist_m * 0.5:
            to_home = math.atan2(home[0] - lat, (home[1] - lng) * math.cos(math.radians(lat)))
            heading += 0.25 * math.atan2(math.sin(to_home - heading), math.cos(to_home - heading))
        heading += rng.uniform(-wiggle, wiggle)
        lat += math.sin(heading) * step / 111_320
        lng += math.cos(heading) * step / (111_320 * math.cos(math.radians(lat)))
        t += step_s
        d += step
    return {"time": times, "distance": dists, "latlng": pts}


def main():
    (CACHE / "tracks").mkdir(parents=True, exist_ok=True)
    index = {}
    today = dt.date.fromisoformat(os.environ.get("TODAY", dt.date.today().isoformat()))
    day = dt.date(today.year - 3, 1, 1)
    n = 0
    while day <= today:
        roll = rng.random()
        kind = None
        if day.weekday() == 6 and roll < 0.85:
            kind = "trail"
        elif day.weekday() in (1, 3) and roll < 0.8:
            kind = "run"
        elif day.weekday() == 5 and roll < 0.75:
            kind = "ride"
        elif day.weekday() == 2 and roll < 0.6:
            kind = "swim"
        elif roll < 0.25:
            kind = "run"
        if kind:
            n += 1
            fitness = 1 + 0.04 * (day.year - (today.year - 3))  # gets a little fitter each year
            hour = rng.choice([6, 7, 17, 18])
            start = dt.datetime.combine(day, dt.time(hour, rng.choice([0, 10, 30, 45])))
            if kind == "run":
                dist, speed, spot, typ, elev = rng.uniform(8, 16) * 1000, 3.3 * fitness, rng.choice(["fraser", "campbell"]), "Run", rng.uniform(30, 120)
            elif kind == "trail":
                dist, speed, spot, typ, elev = rng.uniform(15, 32) * 1000, 2.6 * fitness, rng.choice(["northshore", "campbell", "fraser"]), "TrailRun", rng.uniform(300, 1500)
            elif kind == "ride":
                dist, speed, spot, typ, elev = rng.uniform(40, 110) * 1000, 8.0 * fitness, "glenvalley", "Ride", rng.uniform(150, 900)
            else:
                dist, speed, spot, typ, elev = rng.uniform(1.8, 3.2) * 1000, 0.9, None, "Swim", 0
            name = rng.choice(NAMES.get(kind, ["Pool swim"])) if kind != "swim" else "Pool swim"
            streams = track(SPOTS[spot], dist, speed, wiggle=0.45 if kind == "trail" else 0.3) if spot else {
                "time": [i * 10 for i in range(int(dist / 9))], "distance": [i * 9.0 for i in range(int(dist / 9))]}
            moving = streams["time"][-1] if streams["time"] else dist / speed
            summary = {"id": f"s{n}", "name": name, "type": typ, "start_date_local": start.isoformat(),
                       "distance": round(dist), "moving_time": round(moving), "total_elevation_gain": round(elev),
                       "average_heartrate": rng.randint(132, 156) if kind != "swim" else None,
                       "icu_training_load": round(moving / 3600 * rng.uniform(45, 85))}
            rec, tr = process_activity(summary, streams)
            index[rec["id"]] = rec
            (CACHE / "tracks" / f"{rec['id']}.json").write_text(json.dumps(tr, separators=(",", ":")))
        day += dt.timedelta(days=1)

    # A few standout days so the PB board has something to show
    specials = [
        ("BMO Vancouver Marathon", "Run", dt.datetime(today.year - 3, 5, 7, 7), 42195, 4.02, "fraser", 210),
        ("Fort Langley 10K", "Run", dt.datetime(today.year - 1, 4, 13, 9), 10000, 4.3, "fraser", 40),
        ("Ultra day", "TrailRun", dt.datetime(today.year - 2, 9, 14, 6), 82000, 2.3, "northshore", 3240),
        ("Langley 5K", "Run", dt.datetime(today.year, 6, 8, 8), 5000, 4.5, "campbell", 25),
    ]
    for name, typ, start, dist, speed, spot, elev in specials:
        n += 1
        streams = track(SPOTS[spot], dist, speed, wiggle=0.25)
        summary = {"id": f"s{n}", "name": name, "type": typ, "start_date_local": start.isoformat(), "distance": dist,
                   "moving_time": streams["time"][-1], "total_elevation_gain": elev, "average_heartrate": 165, "race": True,
                   "icu_training_load": round(streams["time"][-1] / 3600 * 95)}
        rec, tr = process_activity(summary, streams)
        index[rec["id"]] = rec
        (CACHE / "tracks" / f"{rec['id']}.json").write_text(json.dumps(tr, separators=(",", ":")))

    (CACHE / "activities.json").write_text(json.dumps(index, separators=(",", ":")))
    (CACHE / "meta.json").write_text(json.dumps({"sample": True}))
    print(f"Wrote {len(index)} sample activities to {CACHE}")


if __name__ == "__main__":
    main()
