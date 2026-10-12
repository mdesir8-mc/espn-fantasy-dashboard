"""Builds a synthetic snapshot shaped like real ESPN/NFL/Sleeper responses, for offline tests and demo mode.

    python3 tests/make_fixture.py tests/fixture.json
    python3 fantasy_tracker.py --demo tests/fixture.json
"""
import json
import random
import sys
import time

random.seed(7)
YEAR, WEEK, N = 2026, 5, 10
NOW = time.time()
PRO = [(1, "ATL"), (2, "BUF"), (3, "CHI"), (4, "CIN"), (5, "CLE"), (6, "DAL"), (7, "DEN"), (8, "DET"), (9, "GB"),
       (10, "TEN"), (11, "IND"), (12, "KC"), (13, "LV"), (14, "LAR"), (15, "MIA"), (16, "MIN"), (17, "NE"), (18, "NO"),
       (19, "NYG"), (20, "NYJ"), (21, "PHI"), (22, "ARI"), (23, "PIT"), (24, "LAC"), (25, "SF"), (26, "SEA"), (27, "TB"),
       (28, "WSH"), (29, "CAR"), (30, "JAX"), (33, "BAL"), (34, "HOU")]
BYES = {12: 5, 29: 5, 24: 7, 28: 7, 19: 8, 18: 8, 8: 6, 33: 7}
POS = {1: ([0, 7, 20, 21], 17), 2: ([2, 3, 23, 7, 20, 21], 12), 3: ([3, 4, 5, 23, 7, 20, 21], 12),
       4: ([5, 6, 23, 7, 20, 21], 9), 5: ([17, 20, 21], 8), 16: ([16, 20, 21], 7)}
SLOTS = {"0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 7, "21": 1, "23": 1}

# games this week: pair up teams not on bye; first third final, next third live, rest upcoming
playing = [t for t, _ in PRO if BYES.get(t) != WEEK]
random.shuffle(playing)
pairs = [(playing[i], playing[i + 1]) for i in range(0, len(playing) - 1, 2)]
state_of = {}
events = []
abbr = dict(PRO)
for i, (h, a) in enumerate(pairs):
    st = "post" if i < 6 else "in" if i < 10 else "pre"
    hs, as_ = (random.randint(10, 35), random.randint(10, 35)) if st != "pre" else (0, 0)
    kickoff = NOW - 4 * 3600 if st == "post" else NOW - 5400 if st == "in" else NOW + 3 * 3600
    for t in (h, a):
        state_of[t] = st
    ev = {"id": str(401872900 + i), "date": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(kickoff)),
          "status": {"period": 3 if st == "in" else 4, "displayClock": "6:12" if st == "in" else "0:00",
                     "type": {"state": st, "completed": st == "post",
                              "shortDetail": {"post": "Final", "in": "6:12 - 3rd", "pre": "10/11 - 8:20 PM EDT"}[st]}},
          "competitions": [{"competitors": [
              {"homeAway": "home", "score": str(hs), "team": {"id": str(h), "abbreviation": abbr[h]}},
              {"homeAway": "away", "score": str(as_), "team": {"id": str(a), "abbreviation": abbr[a]}}],
              "odds": [{"details": f"{abbr[h]} -3.5", "overUnder": 45.5, "spread": -3.5,
                        "homeTeamOdds": {"favorite": True}, "awayTeamOdds": {"favorite": False}}] if st == "pre" else [],
              "situation": {"isRedZone": i == 7, "possession": str(h)} if st == "in" else None}]}
    events.append(ev)

pid = [1000]


def player(name, pos, team, proj, inj="ACTIVE"):
    pid[0] += 1
    st = state_of.get(team, "bye")
    elig, avg = POS[pos]
    actual = round(proj * random.uniform(0.3, 1.8), 1)
    if st == "in":
        actual = round(actual * 0.6, 1)
    stats = [{"id": f"11{YEAR}{WEEK}", "scoringPeriodId": WEEK, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": proj if st != "bye" else 0},
             {"id": f"00{YEAR}", "scoringPeriodId": 0, "statSourceId": 0, "statSplitTypeId": 0, "appliedTotal": avg * 4, "appliedAverage": round(avg * random.uniform(.6, 1.4), 2)},
             {"id": f"10{YEAR}", "scoringPeriodId": 0, "statSourceId": 1, "statSplitTypeId": 0, "appliedTotal": avg * 13, "appliedAverage": round(avg * random.uniform(.7, 1.3), 2)}]
    if st in ("in", "post"):
        stats.append({"id": "0" + "1401872900", "scoringPeriodId": WEEK, "statSourceId": 0, "statSplitTypeId": 1, "appliedTotal": actual})
    return {"id": pid[0], "fullName": name, "defaultPositionId": pos, "eligibleSlots": elig, "proTeamId": team,
            "injuryStatus": inj, "stats": stats,
            "ownership": {"percentOwned": round(random.uniform(5, 99), 1), "percentChange": round(random.uniform(-4, 6), 1), "percentStarted": 50.0}}


def roster(tid):
    teams = [t for t, _ in PRO]
    picks = [(0, 1, 18), (2, 2, 14), (2, 2, 11), (4, 3, 15), (4, 3, 10), (6, 4, 9), (23, 2, 8), (16, 16, 7), (17, 5, 8),
             (20, 1, 15), (20, 2, 12), (20, 3, 13), (20, 4, 8), (20, 2, 6), (20, 3, 5), (20, 3, 4)]
    out = []
    for i, (slot, pos, proj) in enumerate(picks):
        team = random.choice(teams)
        inj = "ACTIVE"
        if tid == 6 and i == 6:
            team, inj = 33, "QUESTIONABLE"   # questionable FLEX whose game is upcoming
        if tid == 6 and i == 0:
            team = next(t for t, _ in PRO if state_of.get(t) == "pre")
        if tid == 6 and i == 9:
            team, proj = next(t for t, _ in PRO if state_of.get(t) == "pre"), 22   # benched QB projecting higher
        if tid == 6 and i == 8:
            team = 12   # kicker on bye
        p = player(f"Player {tid}-{i} {['QB','RB','WR','TE','K','','','','','','','','','','','','D/ST'][pos]}", pos, team, proj, inj)
        out.append({"lineupSlotId": slot, "playerId": p["id"], "playerPoolEntry": {"appliedStatTotal": 0, "player": p}})
    return out


teams = []
names = ["Gridiron Gurus", "Fourth and Long", "End Zone Express", "The Blitz Brigade", "Hail Mary Heroes",
         "Red Zone Raiders", "Two-Minute Drill", "Pigskin Prophets", "Sunday Scaries", "Waiver Wire Wizards"]
ids = [6, 4, 1, 2, 3, 5, 7, 8, 9, 10]
schedule = []
scores = {t: [] for t in ids}
for p in range(1, 15):
    order = ids[:1] + ids[1:][p % 9:] + ids[1:][:p % 9]
    for k in range(N // 2):
        h, a = order[k], order[N - 1 - k]
        m = {"id": len(schedule) + 1, "matchupPeriodId": p, "home": {"teamId": h}, "away": {"teamId": a}}
        if p < WEEK:
            hp, ap = round(random.uniform(70, 160), 2), round(random.uniform(70, 160), 2)
            m["home"]["totalPoints"], m["away"]["totalPoints"] = hp, ap
            m["winner"] = "HOME" if hp > ap else "AWAY"
            scores[h].append((hp, ap))
            scores[a].append((ap, hp))
        else:
            m["winner"] = "UNDECIDED"
            m["home"].update({"totalPoints": 0})
            m["away"].update({"totalPoints": 0})
            if p == WEEK:
                for side in (m["home"], m["away"]):
                    side.update({"totalPointsLive": round(random.uniform(40, 90), 2),
                                 "totalProjectedPointsLive": round(random.uniform(95, 130), 2)})
                m["home"]["winProbability"] = round(random.uniform(.2, .8), 2)
                m["away"]["winProbability"] = round(1 - m["home"]["winProbability"], 2)
        schedule.append(m)
for tid, nm in zip(ids, names):
    w = sum(1 for a, b in scores[tid] if a > b)
    teams.append({"id": tid, "name": nm, "abbrev": "".join(x[0] for x in nm.split())[:4].upper(), "waiverRank": tid,
                  "transactionCounter": {"acquisitions": random.randint(0, 12)},
                  "record": {"overall": {"wins": w, "losses": len(scores[tid]) - w, "ties": 0,
                                         "pointsFor": round(sum(a for a, _ in scores[tid]), 2),
                                         "pointsAgainst": round(sum(b for _, b in scores[tid]), 2),
                                         "streakType": "LOSS", "streakLength": 1}},
                  "roster": {"entries": roster(tid)}})
for i, t in enumerate(sorted(teams, key=lambda t: (-t["record"]["overall"]["wins"], -t["record"]["overall"]["pointsFor"]))):
    t["playoffSeed"] = i + 1

league = {"id": 100001, "seasonId": YEAR, "scoringPeriodId": WEEK,
          "status": {"currentMatchupPeriod": WEEK, "latestScoringPeriod": WEEK, "finalScoringPeriod": 17},
          "settings": {"name": "Demo PPR League", "scheduleSettings": {"playoffTeamCount": 6, "matchupPeriodCount": 14},
                       "rosterSettings": {"lineupSlotCounts": SLOTS}, "scoringSettings": {"scoringType": "H2H_POINTS"}},
          "teams": teams, "schedule": schedule}
pro = {"settings": {"proTeams": [{"id": t, "abbrev": a.lower(), "byeWeek": BYES.get(t, 9 + t % 5),
                                  "proGamesByScoringPeriod": {}} for t, a in PRO]}}
fa = {"players": [{"id": 9000 + i, "status": random.choice(["FREEAGENT", "WAIVERS"]), "waiverProcessDate": (NOW + 86400 * 2) * 1000,
                   "player": player(f"Free Agent {i}", random.choice([1, 2, 3, 3, 4, 5, 16]), random.choice(playing), round(random.uniform(3, 16), 1))}
                  for i in range(40)]}
trending = [{"player_id": "s1", "count": 120345}]
sleeper_players = {"s1": {"full_name": "Free Agent 3", "espn_id": str(fa["players"][3]["player"]["id"]), "team": None}}
history = {str(w): {"scoringPeriod": w, "teams": {str(t): {"actual": scores[t][w - 1][0], "optimal": scores[t][w - 1][0] + random.uniform(0, 30)} for t in ids}}
           for w in range(1, WEEK)}

bundle = {"league": league, "pro": pro, "scoreboard": {"week": {"number": WEEK}, "events": events}, "freeAgents": fa,
          "trending": trending, "sleeperPlayers": sleeper_players, "history": history, "notes": []}
# knockout league: same teams, one "teams" array per week, lowest score among live teams goes out each week
ko_sched, out = [], {}
for p in range(1, 18):
    sides = []
    for t in ids:
        s = {"teamId": t, "totalPoints": 0.0}
        if p < WEEK and t not in out:
            s["totalPoints"] = round(random.uniform(110 if t == 6 else 70, 160), 2)  # keep "you" alive
        elif p == WEEK and t not in out:
            s.update({"totalPointsLive": round(random.uniform(40, 90), 2),
                      "totalProjectedPointsLive": round(random.uniform(85, 130), 2)})
        sides.append(s)
    if p < WEEK:
        cut = min((s for s in sides if s["teamId"] not in out), key=lambda s: s["totalPoints"])
        out[cut["teamId"]] = p
    elif p == WEEK:
        live = sorted((s for s in sides if s["teamId"] not in out), key=lambda s: s["totalProjectedPointsLive"])
        for i, s in enumerate(live):
            s["winProbability"] = round(min(0.99, 0.05 + 0.25 * i), 2)
            s["eliminationProbabilityStatus"] = "DANGER" if i == 0 else "CAUTION" if i == 1 else "SAFE"
    ko_sched.append({"id": p, "matchupPeriodId": p, "teams": sides})
for p_sides in ko_sched:
    for s in p_sides["teams"]:
        s["eliminationMatchupPeriod"] = out.get(s["teamId"], 0)
ko_teams = [{**t, "eliminated": t["id"] in out, "eliminationMatchupPeriod": out.get(t["id"], 0)} for t in teams]
ko_league = {**league, "id": 100002, "teams": ko_teams, "schedule": ko_sched,
             "settings": {**league["settings"], "name": "Demo Knockout League",
                          "scheduleSettings": {"playoffTeamCount": 0, "matchupPeriodCount": 17},
                          "scoringSettings": {"scoringType": "WEEKLY_ALL_PLAY_POINTS"}}}
ko_bundle = {**bundle, "league": ko_league, "history": {}}

snap = [{"cfg": {"name": "Demo league", "league_id": 100001, "team_id": 6, "year": YEAR}, "bundle": bundle, "takenAt": NOW},
        {"cfg": {"name": "Demo knockout", "league_id": 100002, "team_id": 6, "year": YEAR}, "bundle": ko_bundle, "takenAt": NOW},
        {"cfg": {"name": "Broken league", "league_id": 1, "team_id": 99, "year": YEAR}, "bundle": bundle, "takenAt": NOW}]
json.dump(snap, open(sys.argv[1] if len(sys.argv) > 1 else "fixture.json", "w"))
print("wrote fixture")
