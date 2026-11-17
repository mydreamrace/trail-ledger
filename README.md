# Trail Ledger

A personal training site built from my Coros watch. Every 12 hours a GitHub Action pulls new activities from Intervals.icu, works out PBs and training trends, and redeploys the site on GitHub Pages.

```
Coros watch → Intervals.icu → GitHub Action (every 12 h) → GitHub Pages
```

## What's on the page

One page, `site/v2.html`, published as `index.html` (the main page) and `poster.html`.

- 2026 season totals and a heatmap of every run and ride this year
- Form today, training focus, resting HR, HRV, sleep and local weather
- Race bibs: countdowns with readiness lights and bars against race-day targets
- The year day by day as a calendar, plus the sport mix
- Records for running, biking (power) and swimming, all time and this year
- The last 40 routes drawn as shapes

## Poster view (framed screen)

`poster.html` is the same page laid out on a fixed 1080 x 1920 portrait canvas for a monitor turned on its side and framed. It never scrolls, hides the buttons and the mouse pointer, shows as many whole rows of route shapes as fit, scales itself to any screen size, and reloads every 30 minutes.

To run it on a Raspberry Pi: rotate the display to portrait in Screen Configuration, turn off screen blanking in `raspi-config` (Display Options), and open the page full-screen at login by adding this line to `~/.config/labwc/autostart`:

```
chromium --kiosk --noerrdialogs --disable-infobars --incognito https://mydreamrace.github.io/trail-ledger/poster.html
```

(On older Raspberry Pi OS the browser is called `chromium-browser`.)

## One-time setup

### 1. Intervals.icu

1. Connect Coros under Settings > Connections. Connect Coros directly rather than through Strava, because the Intervals API does not return Strava-imported activities.
2. In Settings > Developer, generate an API key. Copy it and your athlete ID (starts with `i`).

### 2. Create the repo

Create a new **public** repository (GitHub Pages needs a public repo on a free account) and push these files to the `main` branch. No activity data ever goes in the repo, so public is fine.

### 3. Add three secrets

Repo > Settings > Secrets and variables > Actions > New repository secret:

| Name | Value |
| --- | --- |
| `INTERVALS_API_KEY` | the key from Intervals.icu |
| `INTERVALS_ATHLETE_ID` | e.g. `i123456` |
| `PRIVACY_ZONES` | JSON list of places to hide, see below |

`PRIVACY_ZONES` looks like `privacy.example.json`. Any route point within `radius_m` metres of a zone is cut out before publishing. To get coordinates, right-click your house in Google Maps and click the numbers at the top of the menu to copy them. 500 m around home is a sensible minimum. The build refuses to publish if this secret is missing.

### 4. Turn on Pages

Repo > Settings > Pages > Build and deployment > Source: **GitHub Actions**.

### 5. First run

Repo > Actions > Twice-daily update > Run workflow. The first run downloads your whole history and can take 20 to 60 minutes. After that, each run takes under a minute.

Your site will be at `https://<your-username>.github.io/<repo-name>/`.

## Day to day

- Runs every 12 hours, around 2 AM and 2 PM Pacific (3 AM and 3 PM in summer). Hit **Run workflow** on the Actions tab (works on the GitHub phone app) to refresh right after a race.
- Race goals, title and map settings live in `config.json`. Edit it on github.com and the site rebuilds on save. Set `date_confirmed` to `true` once a race date is announced.
- Each race in `config.json` has a `readiness` list: up to three bars on its countdown card. `target` is where you want your last 4 weeks of training to be by race day. The marker on each bar shows where you'd typically be at this many weeks out (about half at 30+ weeks, rising to the full target 3 weeks out). Available metrics: `swims_per_week`, `rides_per_week`, `ride_hours_per_week`, `long_ride_km`, `run_km_per_week`, `run_hours_per_week`, `long_run_km`, `climb_per_week`.
- `zwift.category` is shown in the Power row. Zwift doesn't share it, so update it here when it changes. `weight_kg` is used for W/kg when your watch doesn't send weight to Intervals.
- `map.radius_km` sets how much area the heatmap covers around where most of your activities start. Set `map.center` to `[lat, lng]` to pin it somewhere specific.

## Running it locally

```
python scripts/sample_data.py      # fake history in data/cache, no API key needed
python scripts/build.py            # writes public/
python -m http.server -d public    # open http://localhost:8000
```

With real data, set `INTERVALS_API_KEY` and `INTERVALS_ATHLETE_ID`, run `python scripts/fetch.py`, put your zones in `privacy.local.json`, then build.

## How the data stays private

- Raw activities sit in the GitHub Actions cache, which is not public, and are reduced to a simplified route plus best efforts as soon as they are downloaded.
- Home and work coordinates are a secret, never in the code.
- The published site only contains routes with privacy zones removed, and asks search engines not to index it.
- If the cache is ever cleared (GitHub drops caches unused for 7 days), the next run quietly re-downloads everything.

## Files

| Path | Purpose |
| --- | --- |
| `scripts/fetch.py` | pulls new activities and GPS tracks from Intervals.icu |
| `scripts/build.py` | turns the cache into `public/data/stats.json` and the published pages |
| `scripts/lib.py` | best efforts, route simplifying, privacy trimming, polyline encoding |
| `scripts/sample_data.py` | fake data for testing |
| `site/v2.html` | the page itself (main page and poster view) |
| `config.json` | name, races, map settings |
| `.github/workflows/nightly.yml` | the twice-daily job |
