#!/usr/bin/env python3
"""ESPN fantasy football data layer and command-line summary.

    python3 fantasy_tracker.py              # text summary for the leagues in leagues.json
    python3 fantasy_tracker.py --json       # dashboard data as JSON
    python3 fantasy_tracker.py --snapshot snap.json   # save raw responses (for debugging or --demo)
    python3 fantasy_tracker.py --demo snap.json       # replay a saved snapshot, no network

The web dashboard is webapp.py. Sources: ESPN's fantasy API (league, rosters, projections, win probability, free
agents, NFL schedule), ESPN's public NFL scoreboard (game clock, red zone, betting lines) and Sleeper's public API
(players trending on waivers). Standard library only. The ESPN endpoints are unofficial.
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import metrics

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "leagues.json"
CACHE_DIR = Path(os.environ.get("CACHE_DIR") or HERE / ".cache")

FANTASY = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}"
LEAGUE = FANTASY + "/segments/0/leagues/{id}"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?seasontype=2&week={week}&dates={year}"
SLEEPER_TRENDING = "https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=24&limit=100"
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
UA = {"User-Agent": "Mozilla/5.0 (fantasy-tracker)", "Accept": "application/json"}


# ---------------------------------------------------------------- plumbing

def get_json(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and "fantasy.espn.com" in url:
            raise RuntimeError("ESPN says this league is private. Add espn_s2 and swid to its entry in leagues.json.")
        if e.code == 404 and "fantasy.espn.com" in url:
            raise RuntimeError("League not found. Check league_id and year in leagues.json.")
        raise RuntimeError(f"HTTP {e.code} from {url.split('/')[2]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach {url.split('/')[2]} ({e.reason})")


_mem, _mem_lock = {}, threading.Lock()


def cached(key, ttl, fn):
    with _mem_lock:
        hit = _mem.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    with _mem_lock:
        if len(_mem) > 5000:  # many users share one process; drop anything older than the longest TTL
            for k in [k for k, v in _mem.items() if time.time() - v[0] > 6 * 3600]:
                del _mem[k]
        _mem[key] = (time.time(), val)
    return val


def write_atomic(path, text):
    tmp = path.with_name(path.name + f".{threading.get_ident()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def disk_cached(name, ttl, fn):
    path = CACHE_DIR / name
    if path.exists() and (ttl is None or time.time() - path.stat().st_mtime < ttl):
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    val = fn()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(val))
    return val


def cookie_id(cfg):
    """Short hash of the ESPN cookies, so cached private-league responses are only reused by the same credentials."""
    h = cookie_header(cfg).get("Cookie")
    return hashlib.sha256(h.encode()).hexdigest()[:16] if h else ""


def cookie_header(cfg):
    s2, swid = cfg.get("espn_s2"), cfg.get("swid")
    return {"Cookie": f"espn_s2={s2}; SWID={swid}"} if s2 and swid else {}


def load_config():
    raw = os.environ.get("LEAGUES_JSON")  # hosted deploys pass leagues.json through the environment
    if not raw and not CONFIG.exists():
        sys.exit(f"Missing {CONFIG}. See README.md for the format.")
    leagues = json.loads(raw or CONFIG.read_text())["leagues"]
    for c in leagues:
        if c.get("sport", "football") != "football":
            sys.exit(f"{c.get('name') or c['league_id']}: only football is supported right now.")
    return leagues


# ---------------------------------------------------------------- sources

def fetch_league(cfg):
    url = LEAGUE.format(year=cfg["year"], id=cfg["league_id"])
    url += "?view=mTeam&view=mRoster&view=mMatchupScore&view=mSettings&view=mStatus"
    return cached(("league", cfg["league_id"], cfg["year"], cookie_id(cfg)), 20, lambda: get_json(url, cookie_header(cfg)))


def fetch_pro_schedule(year):
    return cached(("pro", year), 6 * 3600, lambda: get_json(FANTASY.format(year=year) + "?view=proTeamSchedules_wl"))


def fetch_scoreboard(year, week):
    return cached(("sb", year, week), 20, lambda: get_json(SCOREBOARD.format(year=year, week=week)))


def fetch_free_agents(cfg, week):
    year = cfg["year"]
    filt = {"players": {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
        "filterSlotIds": {"value": [0, 2, 4, 6, 16, 17, 23]},
        "limit": 120,
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        "filterStatsForTopScoringPeriodIds": {"value": 2, "additionalValue": [f"00{year}", f"10{year}", f"11{year}{week}"]},
    }}
    url = LEAGUE.format(year=year, id=cfg["league_id"]) + f"?view=kona_player_info&scoringPeriodId={week}"
    headers = {"X-Fantasy-Filter": json.dumps(filt), **cookie_header(cfg)}
    return cached(("fa", cfg["league_id"], week, cookie_id(cfg)), 300, lambda: get_json(url, headers))


def fetch_sleeper():
    trending = cached(("sleeper-trending",), 1800, lambda: get_json(SLEEPER_TRENDING))

    def slim_players():  # the full file is ~5 MB; keep the four fields we use
        full = get_json(SLEEPER_PLAYERS, timeout=60)
        return {pid: {"full_name": p.get("full_name"), "espn_id": p.get("espn_id"), "team": p.get("team"),
                      "position": p.get("position")} for pid, p in full.items()}
    players = disk_cached("sleeper_players.json", 24 * 3600, slim_players)
    return trending, players


def fetch_history(cfg, raw):
    """Lineup efficiency for finished weeks. Each week is fetched once, then read from .cache."""
    year, lid = cfg["year"], cfg["league_id"]
    path = CACHE_DIR / f"history_{lid}_{year}.json"
    hist = json.loads(path.read_text()) if path.exists() else {}
    week = raw.get("scoringPeriodId") or 1
    slots = raw["settings"]["rosterSettings"]["lineupSlotCounts"]
    first = (raw.get("status") or {}).get("firstScoringPeriod") or 1
    changed = False
    for sp in range(first, week):
        old = hist.get(str(sp))
        if old and (old.get("teams") or old.get("v")):  # older versions saved knockout weeks empty; redo those
            continue
        box = get_json(LEAGUE.format(year=year, id=lid) + f"?view=mBoxscore&view=mMatchupScore&scoringPeriodId={sp}",
                       cookie_header(cfg))
        hist[str(sp)] = metrics.week_efficiency(box, sp, slots)
        changed = True
    if changed:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(hist))
    return hist


def gather(cfg):
    """All raw inputs for one league. Optional sources fail soft and are reported in `notes`."""
    cfg = {**cfg, "year": cfg.get("year") or time.localtime().tm_year}
    league = fetch_league(cfg)
    week = league.get("scoringPeriodId") or 1
    bundle, notes = {"league": league}, []

    def soft(label, key, fn):
        try:
            bundle[key] = fn()
        except Exception as e:
            notes.append(f"{label} unavailable: {e}")
    soft("NFL schedule", "pro", lambda: fetch_pro_schedule(cfg["year"]))
    soft("Live NFL scoreboard", "scoreboard", lambda: fetch_scoreboard(cfg["year"], week))
    soft("Free agents", "freeAgents", lambda: fetch_free_agents(cfg, week))
    soft("Lineup history", "history", lambda: fetch_history(cfg, league))
    try:
        bundle["trending"], bundle["sleeperPlayers"] = fetch_sleeper()
    except Exception as e:
        notes.append(f"Sleeper trends unavailable: {e}")
    bundle["notes"] = notes
    return cfg, bundle


def build(cfg, bundle, now=None):
    try:
        out = metrics.build_league(bundle, cfg, now or time.time())
        out["notes"] = bundle.get("notes", [])
        return out
    except Exception as e:
        return {"label": cfg.get("name") or str(cfg.get("league_id")), "error": f"Could not read league data: {e}"}


def collect():
    def one(cfg):
        try:
            cfg2, bundle = gather(cfg)
            return build(cfg2, bundle)
        except Exception as e:  # one bad league should not take down the dashboard
            return {"label": cfg.get("name") or str(cfg.get("league_id")), "error": str(e)}
    configs = load_config()
    with ThreadPoolExecutor(max_workers=4) as ex:
        return list(ex.map(one, configs))


# ---------------------------------------------------------------- output

def print_text(data):
    for d in data:
        if "error" in d:
            print(f"\n{d['label']}: {d['error']}")
            continue
        t, m, ko = d["team"], d["matchup"], d.get("knockout")
        if ko:
            print(f"\n{d['label']}  |  {t['name']}  week {d['week']}  {ko['alive']} of {ko['teams']} alive")
            w = ko["week"]
            if ko["outWeek"]:
                print(f"  Eliminated in week {ko['outWeek']}")
            elif w:
                odds = f"{w['survival'] * 100:.0f}%" if w["survival"] is not None else "?"
                print(f"  {w['live']} live, projected {w['proj']} ({w['rank']} of {ko['alive']})   survive {odds}"
                      + (f"   cushion {w['cushion']:+.1f} over {w['line']['name']}" if w["line"] else ""))
        else:
            print(f"\n{d['label']}  |  {t['name']}  {t['record']}  seed {t['seed']}/{t['teams']}  week {d['week']}")
        if m:
            wp = f"{m['winProb'] * 100:.0f}%" if m["winProb"] is not None else "?"
            print(f"  {m['my']} - {m['their']} vs {m['opponent']}   projected {m['myProj']} - {m['theirProj']}   win {wp}")
        for a in d["alerts"]:
            print(f"  [{a['level']}] {a['text']}. {a['detail']}")
        for s in d["lineup"]["starters"]:
            print(f"  {s['slot']:<6}{s['name']:<26}{s['team']:<5}{str(s['pts']):>6} / {s['proj']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="text summary (the default)")
    ap.add_argument("--json", action="store_true", help="print dashboard data as JSON")
    ap.add_argument("--snapshot", metavar="FILE", help="save raw responses for every league to FILE")
    ap.add_argument("--demo", metavar="FILE", help="use a saved snapshot instead of live data")
    a = ap.parse_args()

    if a.snapshot:
        snap = [dict(zip(("cfg", "bundle"), gather(c))) | {"takenAt": time.time()} for c in load_config()]
        Path(a.snapshot).write_text(json.dumps(snap))
        print(f"Saved {len(snap)} league(s) to {a.snapshot}")
        return
    if a.demo:
        snap = json.loads(Path(a.demo).read_text())
        def source():
            return [build(s["cfg"], s["bundle"], s.get("takenAt")) for s in snap]
    else:
        source = collect
    if a.json:
        print(json.dumps(source(), indent=2))
        return
    print_text(source())


if __name__ == "__main__":
    main()
