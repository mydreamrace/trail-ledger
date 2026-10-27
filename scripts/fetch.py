"""Pull new activities from Intervals.icu into data/cache/.

First run downloads your whole history (can take a while). Later runs re-list
everything but only download GPS for new activities, so they finish in seconds.

Needs two environment variables (GitHub secrets in the workflow):
  INTERVALS_API_KEY     from intervals.icu > Settings > Developer
  INTERVALS_ATHLETE_ID  your athlete id, e.g. i123456
"""
import base64
import datetime as dt
import http.client
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from lib import parse_streams, process_activity  # noqa: E402

BASE = "https://intervals.icu/api/v1"
CACHE = pathlib.Path(os.environ.get("CACHE_DIR", "data/cache"))
FIRST_DATE = os.environ.get("HISTORY_START", "2010-01-01")


def api_get(path, params=None):
    key = (os.environ.get("INTERVALS_API_KEY") or "").strip()
    if not key:
        sys.exit("INTERVALS_API_KEY is not set. Add it as a repository secret.")
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    token = base64.b64encode(f"API_KEY:{key}".encode()).decode()
    for attempt in range(6):
        req = urllib.request.Request(url, headers={
            "Authorization": "Basic " + token,
            "Accept": "application/json",
            "User-Agent": "trail-ledger/1.0 (personal stats site; github.com/mydreamrace/trail-ledger)",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("Intervals.icu did not accept the API key (HTTP 401). Generate a new key and update "
                         "the INTERVALS_API_KEY secret.")
            if e.code == 403:
                sys.exit("Intervals.icu accepted the key but refused access to this athlete (HTTP 403). "
                         "Check INTERVALS_ATHLETE_ID matches the account the key came from (it starts with 'i'), "
                         "or set it to 0 to mean 'the owner of this key'.")
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(min(60, 2 ** attempt * 2))
                continue
            raise
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, OSError):
            # dropped connections, resets and timeouts: wait and try again
            time.sleep(min(60, 2 ** attempt * 2))
    raise RuntimeError("Gave up on " + path)


def main():
    athlete = (os.environ.get("INTERVALS_ATHLETE_ID") or "").strip()
    if not athlete:
        sys.exit("INTERVALS_ATHLETE_ID is not set. Add it as a repository secret.")
    (CACHE / "tracks").mkdir(parents=True, exist_ok=True)
    index_path = CACHE / "activities.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}

    # Always list the full history. Listing is cheap (one request per year), and it catches
    # activities Intervals.icu backfills into older dates, e.g. while a Coros import is still running.
    # GPS tracks are only downloaded for activities we haven't seen before.
    oldest = FIRST_DATE
    if not index:
        print("No cache found, downloading full history from", oldest)
    newest = (dt.date.today() + dt.timedelta(days=1)).isoformat()

    # Ask in yearly chunks so a long history doesn't time out in one request.
    found = []
    start = dt.date.fromisoformat(oldest)
    end = dt.date.fromisoformat(newest)
    while start <= end:
        stop = min(end, start + dt.timedelta(days=365))
        batch = api_get(f"/athlete/{athlete}/activities", {"oldest": start.isoformat(), "newest": stop.isoformat()}) or []
        found += batch
        start = stop + dt.timedelta(days=1)

    new = updated = skipped = failed = 0
    for act in found:
        aid = str(act.get("id"))
        # Activities Intervals imported from Strava come back as empty stubs (Strava's API terms).
        if act.get("source") == "STRAVA" and not act.get("distance") and not act.get("moving_time"):
            skipped += 1
            continue
        track_path = CACHE / "tracks" / f"{aid}.json"
        if aid in index and track_path.exists():
            rec, _ = process_activity(act, None)  # refresh name/type, keep cached route
            if rec != index[aid]:
                index[aid] = rec
                updated += 1
            continue
        try:
            raw = api_get(f"/activity/{aid}/streams.json", {"types": "time,distance,latlng"})
        except RuntimeError:
            failed += 1  # not saved, so the next run tries this one again
            continue
        rec, track = process_activity(act, parse_streams(raw))
        index[aid] = rec
        track_path.write_text(json.dumps(track, separators=(",", ":")))
        new += 1
        if new % 50 == 0:
            print(f"  {new} activities downloaded...")
            index_path.write_text(json.dumps(index, separators=(",", ":")))
        time.sleep(0.15)

    index_path.write_text(json.dumps(index, separators=(",", ":")))
    print(f"Done: {new} new, {updated} updated, {len(index)} total in cache.")
    if failed:
        print(f"{failed} activities couldn't be downloaded this time. They'll be retried on the next run.")

    # Fitness (CTL), fatigue (ATL) and ramp rate, as Intervals.icu calculates them, for the last ~4 months.
    w_oldest = (dt.date.today() - dt.timedelta(days=120)).isoformat()
    try:
        wellness = api_get(f"/athlete/{athlete}/wellness", {"oldest": w_oldest, "newest": dt.date.today().isoformat()}) or []
        extra = {"vo2max": "vo2max", "rhr": "restingHR", "hrv": "hrv", "sleep_s": "sleepSecs", "weight": "weight"}
        keep = []
        for w in wellness:
            if not isinstance(w, dict) or not w.get("id"):
                continue
            row = {"date": w.get("id"), "ctl": w.get("ctl"), "atl": w.get("atl"), "ramp": w.get("rampRate")}
            for ours, theirs in extra.items():
                if w.get(theirs) is not None:
                    row[ours] = w[theirs]
            keep.append(row)
        (CACHE / "wellness.json").write_text(json.dumps(keep, separators=(",", ":")))
        have = {k: sum(1 for r in keep if k in r) for k in extra}
        print(f"Saved {len(keep)} days of fitness data. Days with: "
              + ", ".join(f"{k} {n}" for k, n in have.items()))
    except Exception as e:  # fitness is a nice-to-have; the site falls back to its own estimate
        print("Couldn't fetch fitness data:", e)
    if skipped:
        print(f"Skipped {skipped} Strava-imported activities. Connect Coros directly in Intervals.icu to include them.")


if __name__ == "__main__":
    main()
