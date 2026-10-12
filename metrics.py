"""Turns raw ESPN, NFL scoreboard and Sleeper payloads into dashboard data.

Pure functions only (no network) so the whole thing can be tested against saved responses.
"""
import math
import re
import statistics
from datetime import datetime

# ESPN football lineup slot ids and default position ids
SLOT_NAMES = {0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE", 7: "OP", 8: "DT", 9: "DE",
              10: "LB", 11: "DL", 12: "CB", 13: "S", 14: "DB", 15: "DP", 16: "D/ST", 17: "K", 18: "P", 19: "HC",
              20: "BE", 21: "IR", 23: "FLEX", 24: "ER"}
POS_NAMES = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 7: "P", 9: "DT", 10: "DE", 11: "LB", 12: "CB", 13: "S",
             14: "HC", 16: "D/ST"}
BENCH, IR = 20, 21
NOT_STARTING = {BENCH, IR}
# Strict slots are filled before flex slots, which makes greedy assignment optimal for nested flex rules.
FILL_ORDER = [0, 2, 4, 6, 16, 17, 18, 19, 8, 9, 10, 12, 13, 11, 14, 1, 3, 5, 23, 15, 7, 24]
DISPLAY_ORDER = [0, 1, 7, 2, 4, 6, 3, 5, 23, 24, 8, 9, 10, 11, 12, 13, 14, 15, 18, 19, 16, 17]

INJURY_LEVEL = {"OUT": "critical", "INJURY_RESERVE": "critical", "SUSPENSION": "critical",
                "DOUBTFUL": "serious", "QUESTIONABLE": "warning"}
INJURY_LABEL = {"OUT": "Out", "INJURY_RESERVE": "IR", "SUSPENSION": "Suspended", "DOUBTFUL": "Doubtful",
                "QUESTIONABLE": "Questionable", "DAY_TO_DAY": "Day to day"}


def r1(x):
    return None if x is None else round(float(x), 1)


def _stat(player, source, split, period, stat_id=None):
    for s in player.get("stats") or []:
        if (s.get("statSourceId") == source and s.get("statSplitTypeId") == split
                and s.get("scoringPeriodId") == period and (stat_id is None or s.get("id") == stat_id)):
            return s
    return None


def team_name(t):
    return t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip() or f"Team {t.get('id')}"


def wins_of(t):
    o = (t.get("record") or {}).get("overall") or {}
    return o.get("wins", 0) + 0.5 * o.get("ties", 0)


def record_str(t):
    o = (t.get("record") or {}).get("overall") or {}
    s = f"{o.get('wins', 0)}-{o.get('losses', 0)}"
    return s + f"-{o['ties']}" if o.get("ties") else s


# ---------------------------------------------------------------- NFL games

def implied_totals(odds, home_abbrev, away_abbrev):
    """Vegas implied team totals from the over/under and spread: (home, away)."""
    if not odds:
        return None, None
    ou, spread = odds.get("overUnder"), odds.get("spread")
    if ou is None or spread is None:
        return None, None
    s = abs(float(spread))
    if (odds.get("homeTeamOdds") or {}).get("favorite"):
        fav_home = True
    elif (odds.get("awayTeamOdds") or {}).get("favorite"):
        fav_home = False
    else:
        m = re.match(r"([A-Z]+)\s*-", odds.get("details") or "")
        if not m:
            return r1(ou / 2), r1(ou / 2)
        fav_home = m.group(1) == home_abbrev
    fav, dog = (ou + s) / 2, (ou - s) / 2
    return (r1(fav), r1(dog)) if fav_home else (r1(dog), r1(fav))


def build_games(scoreboard, pro, week, now):
    """Map pro team id -> this week's game, preferring the live scoreboard, falling back to ESPN's schedule."""
    teams = {}
    for t in ((pro or {}).get("settings") or {}).get("proTeams") or []:
        teams[t["id"]] = {"abbrev": t.get("abbrev", "").upper(), "bye": t.get("byeWeek"),
                          "games": t.get("proGamesByScoringPeriod") or {}}
    games = {}
    if scoreboard and (scoreboard.get("week") or {}).get("number") == week:
        for ev in scoreboard.get("events") or []:
            comp = (ev.get("competitions") or [{}])[0]
            cs = comp.get("competitors") or []
            if len(cs) != 2:
                continue
            st = ev.get("status") or {}
            typ = st.get("type") or {}
            home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
            away = cs[1] if home is cs[0] else cs[0]
            ha, aa = home["team"].get("abbreviation", ""), away["team"].get("abbreviation", "")
            imp_h, imp_a = implied_totals((comp.get("odds") or [None])[0], ha, aa)
            sit = comp.get("situation") or {}
            kickoff = _iso_ts(ev.get("date"))
            for me, opp, is_home, imp in ((home, away, True, imp_h), (away, home, False, imp_a)):
                tid = int(me["team"]["id"])
                games[tid] = {
                    "state": typ.get("state", "pre"), "detail": typ.get("shortDetail", ""),
                    "period": st.get("period"), "clock": st.get("displayClock"),
                    "opp": opp["team"].get("abbreviation", ""), "home": is_home, "kickoff": kickoff,
                    "score": _int(me.get("score")), "oppScore": _int(opp.get("score")), "implied": imp,
                    "redZone": bool(sit.get("isRedZone")) and str(sit.get("possession")) == str(tid),
                }
    for tid, t in teams.items():
        if tid in games or tid == 0:
            continue
        if t["bye"] == week:
            games[tid] = {"state": "bye", "detail": "Bye"}
            continue
        g = (t["games"].get(str(week)) or [None])[0]
        if not g:
            continue
        kickoff = g["date"] / 1000
        state = "pre" if now < kickoff else ("post" if now > kickoff + 4 * 3600 else "in")
        is_home = g.get("homeProTeamId") == tid
        opp = teams.get(g["awayProTeamId"] if is_home else g["homeProTeamId"], {}).get("abbrev", "")
        games[tid] = {"state": state, "detail": "", "opp": opp, "home": is_home, "kickoff": kickoff,
                      "implied": None, "redZone": False}
    return teams, games


def _iso_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def remaining_fraction(g):
    state = (g or {}).get("state")
    if state in ("pre", None):
        return 1.0
    if state in ("post", "bye"):
        return 0.0
    period = g.get("period") or 1
    if period > 4:
        return 0.02
    try:
        m, s = (g.get("clock") or "15:00").split(":")
        left = int(m) + int(s) / 60
    except ValueError:
        left = 7.5
    return max(0.0, min(1.0, 1 - ((period - 1) * 15 + (15 - left)) / 60))


# ---------------------------------------------------------------- players

def player_row(player, slot_id, ctx):
    week, year = ctx["week"], ctx["year"]
    tid = player.get("proTeamId")
    team = ctx["teams"].get(tid, {})
    g = ctx["games"].get(tid) or ({"state": "bye", "detail": "Bye"} if team.get("bye") == week else {})
    proj_s = _stat(player, 1, 1, week)
    act_s = _stat(player, 0, 1, week)
    season = _stat(player, 0, 0, 0, f"00{year}")
    season_proj = _stat(player, 1, 0, 0, f"10{year}")
    proj = proj_s.get("appliedTotal") if proj_s else None
    pts = act_s.get("appliedTotal") if act_s else None
    state = g.get("state") or ("none" if not tid else "pre")
    frac = remaining_fraction(g)
    if state == "bye" or not tid:
        live_proj = pts or 0.0
    else:
        live_proj = (pts or 0.0) + (proj or 0.0) * frac
    own = player.get("ownership") or {}
    inj = player.get("injuryStatus") or "ACTIVE"
    return {
        "id": player.get("id"), "name": player.get("fullName"),
        "pos": POS_NAMES.get(player.get("defaultPositionId"), "?"),
        "slot": SLOT_NAMES.get(slot_id, "") if slot_id is not None else "",
        "slotId": slot_id, "eligible": [s for s in player.get("eligibleSlots") or [] if s not in NOT_STARTING],
        "team": team.get("abbrev") or ("FA" if not tid else ""), "bye": team.get("bye"),
        "injury": inj, "injuryLabel": INJURY_LABEL.get(inj, ""),
        "game": {k: g.get(k) for k in ("state", "detail", "opp", "home", "kickoff", "score", "oppScore",
                                       "implied", "redZone")} if g else {"state": state},
        "state": state,
        "proj": r1(proj), "pts": r1(pts), "liveProj": r1(live_proj),
        "seasonPts": r1(season.get("appliedTotal")) if season else None,
        "seasonAvg": r1(season.get("appliedAverage")) if season else None,
        "projAvg": r1(season_proj.get("appliedAverage")) if season_proj else None,
        "owned": r1(own.get("percentOwned")), "ownedChange": r1(own.get("percentChange")),
        "started": r1(own.get("percentStarted")),
    }


def roster_rows(team, ctx):
    rows = [player_row(e["playerPoolEntry"]["player"], e.get("lineupSlotId"), ctx)
            for e in (team.get("roster") or {}).get("entries", [])]
    order = {s: i for i, s in enumerate(DISPLAY_ORDER)}
    starters = sorted([r for r in rows if r["slotId"] not in NOT_STARTING], key=lambda r: order.get(r["slotId"], 99))
    bench = sorted([r for r in rows if r["slotId"] == BENCH], key=lambda r: -(r["proj"] or 0))
    ir = [r for r in rows if r["slotId"] == IR]
    return starters, bench, ir


# ---------------------------------------------------------------- lineup optimisation

def best_lineup(players, slot_counts, value, locked=lambda r: False):
    """Best assignment of players to starting slots.

    players need id, slotId, eligible. Locked starters keep their slot, locked bench players stay benched.
    Returns (list of (slotId, player or None), total value).
    """
    open_counts = {int(k): v for k, v in slot_counts.items() if int(k) not in NOT_STARTING and v}
    chosen, used = [], set()
    for r in players:
        if locked(r) and r["slotId"] not in NOT_STARTING and open_counts.get(r["slotId"], 0) > 0:
            open_counts[r["slotId"]] -= 1
            chosen.append((r["slotId"], r))
            used.add(r["id"])
    pool = [r for r in players if r["id"] not in used and not locked(r) and r["slotId"] != IR]
    for sid in FILL_ORDER + [s for s in open_counts if s not in FILL_ORDER]:
        for _ in range(open_counts.get(sid, 0)):
            cands = [r for r in pool if r["id"] not in used and sid in r["eligible"]]
            best = max(cands, key=value, default=None)
            chosen.append((sid, best))
            if best is not None:
                used.add(best["id"])
    return chosen, sum(value(r) for _, r in chosen if r)


def is_locked(r):
    return r["state"] in ("in", "post")


def lineup_advice(starters, bench, slot_counts):
    """Projection-based start/sit suggestions among players whose games have not kicked off."""
    everyone = starters + bench

    def val(r):
        return r["liveProj"] or 0.0

    current = sum(val(r) for r in starters)
    chosen, best = best_lineup(everyone, slot_counts, val, is_locked)
    opt_ids = {r["id"] for _, r in chosen if r}
    cur_ids = {r["id"] for r in starters}
    ins = sorted([r for r in everyone if r["id"] in opt_ids - cur_ids], key=val, reverse=True)
    outs = sorted([r for r in starters if r["id"] in cur_ids - opt_ids], key=val)
    swaps = []
    for i, r in enumerate(ins):
        o = outs[i] if i < len(outs) else None
        swaps.append({"in": r["name"], "inPos": r["pos"], "inProj": r1(val(r)),
                      "out": o["name"] if o else None, "outProj": r1(val(o)) if o else None,
                      "gain": r1(val(r) - (val(o) if o else 0))})
    empty = [SLOT_NAMES.get(s, str(s)) for s, n in _missing_slots(starters, slot_counts)]
    return {"current": r1(current), "best": r1(best), "gain": r1(max(0.0, best - current)),
            "swaps": swaps if best - current >= 0.5 else [], "emptySlots": empty}


def _missing_slots(starters, slot_counts):
    filled = {}
    for r in starters:
        filled[r["slotId"]] = filled.get(r["slotId"], 0) + 1
    for k, v in slot_counts.items():
        k = int(k)
        if k in NOT_STARTING or not v:
            continue
        for _ in range(v - filled.get(k, 0)):
            yield k, 1


def week_efficiency(boxscore, scoring_period, slot_counts):
    """For a finished week: points each team scored vs. the best lineup it could have set."""
    out = {}
    for m in boxscore.get("schedule") or []:
        # knockout leagues list every team in one "teams" array instead of home/away pairs
        for side in m.get("teams") or (m.get("home"), m.get("away")):
            roster = (side or {}).get("rosterForCurrentScoringPeriod")
            if not roster or not roster.get("entries"):
                continue
            players = []
            for e in roster.get("entries", []):
                p = e["playerPoolEntry"]["player"]
                players.append({"id": p["id"], "slotId": e.get("lineupSlotId"),
                                "eligible": [s for s in p.get("eligibleSlots") or [] if s not in NOT_STARTING],
                                "v": e["playerPoolEntry"].get("appliedStatTotal") or 0.0})
            actual = sum(r["v"] for r in players if r["slotId"] not in NOT_STARTING)
            _, best = best_lineup(players, slot_counts, lambda r: r["v"])
            out[str(side["teamId"])] = {"actual": round(actual, 2), "optimal": round(max(best, actual), 2)}
    return {"scoringPeriod": scoring_period, "teams": out, "v": 2}


# ---------------------------------------------------------------- matchups & season

def _normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def side_live(side, rows, final):
    if final:
        return r1(side.get("totalPoints")), r1(side.get("totalPoints"))
    live = side.get("totalPointsLive")
    proj = side.get("totalProjectedPointsLive")
    if live is None:
        live = sum(r["pts"] or 0 for r in rows)
    if proj is None:
        proj = sum(r["liveProj"] or 0 for r in rows)
    return r1(live), r1(proj)


def dots(rows):
    """One entry per starter: done / playing / to play / bye (for the players-left rail)."""
    m = {"post": "done", "in": "playing", "pre": "toplay", "bye": "bye"}
    return [{"name": r["name"], "slot": r["slot"], "s": m.get(r["state"], "toplay"), "pts": r["pts"]} for r in rows]


def matchup_block(sched_m, my_id, starters_by_team, names):
    mine, theirs = (sched_m["home"], sched_m.get("away")) if sched_m["home"]["teamId"] == my_id else \
        (sched_m.get("away"), sched_m["home"])
    if theirs is None:
        return None
    final = sched_m.get("winner") not in (None, "UNDECIDED")
    my_rows = starters_by_team.get(my_id, [])
    op_rows = starters_by_team.get(theirs["teamId"], [])
    my_live, my_proj = side_live(mine, my_rows, final)
    op_live, op_proj = side_live(theirs, op_rows, final)
    wp = mine.get("winProbability")
    if final:
        wp = 1.0 if (my_live or 0) > (op_live or 0) else 0.0 if (my_live or 0) < (op_live or 0) else 0.5
    elif wp is None:
        var = sum((0.45 * (r["proj"] or 0) * remaining_fraction(r["game"])) ** 2 for r in my_rows + op_rows)
        diff = (my_proj or 0) - (op_proj or 0)
        wp = 0.5 if var == 0 and diff == 0 else (1.0 if diff > 0 else 0.0) if var == 0 else _normal_cdf(diff / math.sqrt(var))
    return {"opponentId": theirs["teamId"], "opponent": names.get(theirs["teamId"], ""), "final": final,
            "my": my_live, "their": op_live, "myProj": my_proj, "theirProj": op_proj,
            "winProb": round(wp, 3) if wp is not None else None,
            "myDots": dots(my_rows), "theirDots": dots(op_rows)}


def completed_scores(schedule, current_period):
    """{matchupPeriod: {teamId: points}} for decided matchups before the current period."""
    out = {}
    for m in schedule:
        p = m.get("matchupPeriodId")
        if p is None or p >= current_period or m.get("winner") in (None, "UNDECIDED"):
            continue
        for side in (m.get("home"), m.get("away")):
            if side:
                out.setdefault(p, {})[side["teamId"]] = side.get("totalPoints") or 0.0
    return out


def all_play(scores):
    """teamId -> (wins, losses, ties, expected wins)."""
    res = {}
    for week in scores.values():
        n = len(week)
        for tid, pts in week.items():
            w = sum(1 for o, p in week.items() if o != tid and p < pts)
            l = sum(1 for o, p in week.items() if o != tid and p > pts)
            t = n - 1 - w - l
            a = res.setdefault(tid, [0, 0, 0, 0.0])
            a[0] += w
            a[1] += l
            a[2] += t
            a[3] += (w + 0.5 * t) / (n - 1) if n > 1 else 0
    return res


def remaining_sos(schedule, current_period, last_regular, teams):
    """teamId -> average points-per-game of remaining regular-season opponents."""
    ppg = {}
    for t in teams:
        o = (t.get("record") or {}).get("overall") or {}
        g = o.get("wins", 0) + o.get("losses", 0) + o.get("ties", 0)
        ppg[t["id"]] = (o.get("pointsFor", 0) / g) if g else 0
    opps = {}
    for m in schedule:
        p = m.get("matchupPeriodId", 0)
        if p <= current_period or p > last_regular or not m.get("away"):
            continue
        h, a = m["home"]["teamId"], m["away"]["teamId"]
        opps.setdefault(h, []).append(a)
        opps.setdefault(a, []).append(h)
    return {tid: {"avg": r1(statistics.mean(ppg[o] for o in os)), "opponents": os}
            for tid, os in opps.items() if os}


# ---------------------------------------------------------------- knockout leagues

SURVIVAL_LEVEL = {"DANGER": "critical", "CAUTION": "serious"}


def knockout_block(schedule, teams_raw, my_id, period, last_period, names, starters_by_team):
    """Survivor view for knockout leagues, where the lowest-scoring team still alive is eliminated each week.

    Schedule entries carry a `teams` array. A team's eliminationMatchupPeriod is the week it went out (0 = alive).
    """
    by_period = {m["matchupPeriodId"]: m["teams"] for m in schedule if m.get("teams")}
    out_week = {t["id"]: t.get("eliminationMatchupPeriod") or 0 for t in teams_raw}

    def alive_in(tid, p):
        return not out_week.get(tid) or out_week[tid] >= p

    # finished weeks: my score against the cut line (lowest score among teams alive that week)
    history, season_pts, out_pts = [], {}, {}
    for p in sorted(k for k in by_period if k < period):
        wk = {s["teamId"]: s.get("totalPoints") or 0.0 for s in by_period[p] if alive_in(s["teamId"], p)}
        if not wk or not any(wk.values()):  # weeks before the league started
            continue
        for tid, pts in wk.items():
            season_pts[tid] = season_pts.get(tid, 0.0) + pts
        cut = next((tid for tid in wk if out_week.get(tid) == p), min(wk, key=wk.get))
        out_pts[cut] = wk[cut]
        if my_id not in wk:
            continue
        mine = wk[my_id]
        others = [v for t, v in wk.items() if t != my_id]
        history.append({"week": p, "pts": r1(mine), "line": r1(wk[cut]), "lineTeam": names.get(cut),
                        "median": r1(statistics.median(wk.values())), "high": r1(max(wk.values())),
                        "rank": 1 + sum(1 for v in others if v > mine), "alive": len(wk),
                        "beat": r1(100 * sum(1 for v in others if v < mine) / max(1, len(others))),
                        "cushion": r1(mine - min(others)) if others else None,
                        "result": "Out" if cut == my_id else "Safe"})

    # this week
    rows = []
    for s in by_period.get(period) or []:
        tid = s["teamId"]
        if not alive_in(tid, period):
            continue
        st = starters_by_team.get(tid, [])
        live = s.get("totalPointsLive")
        proj = s.get("totalProjectedPointsLive")
        rows.append({"id": tid, "name": names.get(tid, f"Team {tid}"), "isMe": tid == my_id,
                     "live": r1(live if live is not None else sum(r["pts"] or 0 for r in st)),
                     "proj": r1(proj if proj is not None else sum(r["liveProj"] or 0 for r in st)),
                     "survival": s.get("winProbability"), "status": s.get("eliminationProbabilityStatus"),
                     "seasonPts": r1(season_pts.get(tid)),
                     "left": sum(1 for r in st if r["state"] in ("pre", "in"))})
    rows.sort(key=lambda r: (-(r["proj"] or 0), -(r["live"] or 0)))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        r["liveRank"] = 1 + sum(1 for o in rows if (o["live"] or 0) > (r["live"] or 0))
    me = next((r for r in rows if r["isMe"]), None)
    others = [r for r in rows if not r["isMe"]]
    line = min(others, key=lambda r: (r["proj"] or 0, r["live"] or 0)) if others else None
    week = None
    if me:
        week = {**me, "line": line and {"name": line["name"], "proj": line["proj"], "live": line["live"]},
                "cushion": r1(me["proj"] - line["proj"]) if line else None,
                "cushionLive": r1(me["live"] - line["live"]) if line else None,
                "dots": dots(starters_by_team.get(my_id, []))}

    eliminated = sorted(({"week": w, "id": tid, "name": names.get(tid, f"Team {tid}"), "pts": r1(out_pts.get(tid))}
                         for tid, w in out_week.items() if w), key=lambda e: (e["week"], e["name"]))
    survived = [h for h in history if h["result"] == "Safe"]
    closest = min(survived, key=lambda h: h["cushion"] if h["cushion"] is not None else 1e9, default=None)
    return {
        "alive": len(rows) or sum(1 for t in teams_raw if not t.get("eliminated")), "teams": len(teams_raw),
        "outWeek": out_week.get(my_id) or None, "week": week, "board": rows, "eliminated": eliminated,
        "weeksLeft": max(0, last_period - period + 1), "history": history,
        "summary": {"survived": len(survived),
                    "avgBeat": r1(statistics.mean(h["beat"] for h in history)) if history else None,
                    "avgCushion": r1(statistics.mean(h["cushion"] for h in survived if h["cushion"] is not None))
                    if survived else None,
                    "closest": closest and {"week": closest["week"], "cushion": closest["cushion"],
                                            "team": closest["lineTeam"]}},
    }


def knockout_alerts(ko):
    w = ko and ko.get("week")
    if not w or w.get("cushion") is None:
        return []
    level = SURVIVAL_LEVEL.get(w.get("status"))
    if w["cushion"] < 0:
        level = "critical"
    elif w["cushion"] < 10:
        level = level or "serious"
    if not level:
        return []
    odds = f" ESPN gives you {round(w['survival'] * 100)}% to survive." if w.get("survival") is not None else ""
    line = w["line"]
    if w["cushion"] < 0:
        text = "Projected to be eliminated"
        detail = (f"Projected {w['proj']:.1f}, {-w['cushion']:.1f} behind {line['name']} "
                  f"({line['proj']:.1f}), the next-lowest team.{odds}")
    else:
        text = "Close to the cut line"
        detail = f"Projected {w['proj']:.1f}, {w['cushion']:.1f} above {line['name']} ({line['proj']:.1f}).{odds}"
    return [{"level": level, "text": text, "detail": detail}]


# ---------------------------------------------------------------- waivers

def sleeper_adds(trending, sleeper_players):
    """espn player id -> Sleeper adds in the last 24h, plus a (name, team) fallback key."""
    by_id, by_name = {}, {}
    for t in trending or []:
        p = (sleeper_players or {}).get(str(t.get("player_id")))
        if not p:
            continue
        if p.get("espn_id"):
            by_id[int(p["espn_id"])] = t.get("count", 0)
        by_name[(_norm(p.get("full_name")), (p.get("team") or "").upper())] = t.get("count", 0)
    return by_id, by_name


def _norm(name):
    return re.sub(r"[^a-z]", "", (name or "").lower().replace(" jr", "").replace(" sr", "").replace(" iii", "").replace(" ii", ""))


def waiver_rows(free_agents, ctx, my_starters, slot_counts, adds):
    by_id, by_name = adds
    starting_pos = {POS_NAMES.get(p) for s in slot_counts for p in _slot_positions(int(s))
                    if int(s) not in NOT_STARTING and slot_counts[s]}
    # compare on season-long projected points per game, which outlasts this week's lock status
    worst = {}
    for r in my_starters:
        if r["pos"] not in worst or (r["projAvg"] or 0) < (worst[r["pos"]]["projAvg"] or 0):
            worst[r["pos"]] = r
    rows = []
    for x in (free_agents or {}).get("players") or []:
        p = x.get("player") or {}
        if not p.get("proTeamId"):
            continue
        r = player_row(p, None, ctx)
        if r["pos"] not in starting_pos:
            continue
        r["status"] = "Waivers" if x.get("status") == "WAIVERS" else "Free agent"
        r["waiverClears"] = (x.get("waiverProcessDate") or 0) / 1000 or None
        r["sleeperAdds"] = by_id.get(r["id"]) or by_name.get((_norm(r["name"]), r["team"]))
        w = worst.get(r["pos"])
        r["replaces"] = w["name"] if w else None
        r["upgrade"] = r1((r["projAvg"] or 0) - (w["projAvg"] or 0)) if w and r["projAvg"] is not None else None
        rows.append(r)
    rows.sort(key=lambda r: -(r["projAvg"] or 0))
    return rows


def _slot_positions(slot):
    return {0: [1], 1: [1], 2: [2], 3: [2, 3], 4: [3], 5: [3, 4], 6: [4], 7: [1, 2, 3, 4], 16: [16], 17: [5],
            18: [7], 23: [2, 3, 4]}.get(slot, [])


# ---------------------------------------------------------------- top level

def build_league(bundle, cfg, now):
    raw = bundle["league"]
    my_id = cfg["team_id"]
    teams_raw = raw["teams"]
    by_id = {t["id"]: t for t in teams_raw}
    if my_id not in by_id:
        raise ValueError(f"team_id {my_id} is not in this league (team ids: {sorted(by_id)})")
    week = raw.get("scoringPeriodId") or (raw.get("status") or {}).get("latestScoringPeriod")
    period = (raw.get("status") or {}).get("currentMatchupPeriod") or week
    year = raw.get("seasonId") or cfg.get("year")
    settings = raw.get("settings") or {}
    sched_set = settings.get("scheduleSettings") or {}
    slot_counts = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    playoff_n = sched_set.get("playoffTeamCount") or 0
    last_regular = sched_set.get("matchupPeriodCount") or 99

    pro_teams, games = build_games(bundle.get("scoreboard"), bundle.get("pro"), week, now)
    ctx = {"week": week, "year": year, "teams": pro_teams, "games": games}
    names = {t["id"]: team_name(t) for t in teams_raw}

    lineups = {t["id"]: roster_rows(t, ctx) for t in teams_raw}
    starters_by_team = {tid: v[0] for tid, v in lineups.items()}
    starters, bench, ir = lineups[my_id]
    me = by_id[my_id]

    # Knockout / all-play leagues list a "teams" array instead of home/away pairs; keep only head-to-head entries.
    schedule = [m for m in raw.get("schedule") or [] if m.get("home")]
    current = [m for m in schedule if m.get("matchupPeriodId") == period]
    my_m = next((m for m in current if my_id in (m["home"]["teamId"], (m.get("away") or {}).get("teamId"))), None)
    matchup = matchup_block(my_m, my_id, starters_by_team, names) if my_m else None
    board = []
    for m in current:
        if not m.get("away"):
            continue
        b = matchup_block(m, m["home"]["teamId"], starters_by_team, names)
        board.append({"home": names[m["home"]["teamId"]], "away": b["opponent"], "homeId": m["home"]["teamId"],
                      "awayId": b["opponentId"], "homePts": b["my"], "awayPts": b["their"],
                      "homeProj": b["myProj"], "awayProj": b["theirProj"], "homeWin": b["winProb"],
                      "final": b["final"], "isMine": my_id in (m["home"]["teamId"], b["opponentId"])})

    advice = lineup_advice(starters, bench, slot_counts)
    knockout = None
    if any(m.get("teams") for m in raw.get("schedule") or []):
        knockout = knockout_block(raw["schedule"], teams_raw, my_id, period, last_regular, names, starters_by_team)

    # season figures
    scores = completed_scores(schedule, period)
    ap = all_play(scores)
    weekly = []
    for p in sorted(scores):
        wk = scores[p]
        if my_id not in wk:
            continue
        opp = next((s for m in schedule if m.get("matchupPeriodId") == p
                    for s in (m["home"], m.get("away")) if s and s["teamId"] != my_id
                    and my_id in (m["home"]["teamId"], (m.get("away") or {}).get("teamId"))), None)
        mine = wk[my_id]
        weekly.append({"week": p, "pts": r1(mine), "median": r1(statistics.median(wk.values())),
                       "high": r1(max(wk.values())), "opp": r1(opp["totalPoints"]) if opp else None,
                       "result": "W" if opp and mine > opp["totalPoints"] else "L" if opp and mine < opp["totalPoints"] else "T"})
    history = bundle.get("history") or {}
    eff = {}
    for wk in history.values():
        for tid, v in (wk.get("teams") or {}).items():
            e = eff.setdefault(int(tid), [0.0, 0.0])
            e[0] += v["actual"]
            e[1] += v["optimal"]
    sos = remaining_sos(schedule, period, last_regular, teams_raw)
    sos_rank = {tid: i + 1 for i, (tid, _) in enumerate(sorted(sos.items(), key=lambda kv: -kv[1]["avg"]))}

    seeded = sorted(teams_raw, key=lambda t: (t.get("playoffSeed") or 99))
    pf_sorted = sorted(teams_raw, key=lambda t: -((t.get("record") or {}).get("overall") or {}).get("pointsFor", 0))
    pf_rank = {t["id"]: i + 1 for i, t in enumerate(pf_sorted)}
    standings = []
    for t in seeded:
        o = (t.get("record") or {}).get("overall") or {}
        a = ap.get(t["id"], [0, 0, 0, 0.0])
        e = eff.get(t["id"])
        standings.append({
            "id": t["id"], "seed": t.get("playoffSeed"), "name": names[t["id"]], "isMe": t["id"] == my_id,
            "record": record_str(t), "wins": wins_of(t), "pf": r1(o.get("pointsFor")), "pa": r1(o.get("pointsAgainst")),
            "pfRank": pf_rank[t["id"]], "allPlay": f"{a[0]}-{a[1]}" + (f"-{a[2]}" if a[2] else ""),
            "allPlayPct": r1(100 * (a[0] + 0.5 * a[2]) / max(1, a[0] + a[1] + a[2])),
            "expWins": r1(a[3]), "luck": r1(wins_of(t) - a[3]) if scores else None,
            "efficiency": r1(100 * e[0] / e[1]) if e and e[1] else None,
            "benchPts": r1(e[1] - e[0]) if e else None,
            "streak": f"{(o.get('streakType') or '')[:1]}{o.get('streakLength')}" if o.get("streakLength") else "",
            "sosRank": sos_rank.get(t["id"]), "moves": ((t.get("transactionCounter") or {}).get("acquisitions")),
        })
    mine_st = next(s for s in standings if s["isMe"])
    playoff = None
    if playoff_n:
        line_team = next((s for s in standings if s["seed"] == playoff_n), None)
        first_out = next((s for s in standings if s["seed"] == playoff_n + 1), None)
        inside = (mine_st["seed"] or 99) <= playoff_n
        if inside and first_out:
            margin = mine_st["wins"] - first_out["wins"]
        elif line_team:
            margin = mine_st["wins"] - line_team["wins"]
        else:
            margin = None
        playoff = {"spots": playoff_n, "inside": inside, "margin": margin,
                   "lineTeam": line_team["name"] if line_team else None,
                   "lineRecord": line_team["record"] if line_team else None,
                   "weeksLeft": max(0, last_regular - period + (0 if matchup and matchup["final"] else 1))}
    my_sos = sos.get(my_id)
    remaining = [{"week": p, "opponent": names.get(o), "oppRecord": record_str(by_id[o]),
                  "oppPpg": r1(((by_id[o].get("record") or {}).get("overall") or {}).get("pointsFor", 0)
                               / max(1, sum(((by_id[o].get("record") or {}).get("overall") or {}).get(k, 0)
                                            for k in ("wins", "losses", "ties"))))}
                 for p, o in _remaining_opponents(schedule, my_id, period, last_regular)]

    adds = sleeper_adds(bundle.get("trending"), bundle.get("sleeperPlayers"))
    waivers = waiver_rows(bundle.get("freeAgents"), ctx, starters, slot_counts, adds)

    byes = []
    final_sp = (raw.get("status") or {}).get("finalScoringPeriod") or 17
    for w in range(week + 1, min(week + 5, final_sp + 1)):
        out = [r for r in starters + bench if r["bye"] == w]
        if out:
            byes.append({"week": w, "players": [{"name": r["name"], "pos": r["pos"], "starter": r in starters}
                                                for r in out]})

    alerts = build_alerts(starters, bench, advice, waivers, byes, week, knockout_alerts(knockout))
    o = (me.get("record") or {}).get("overall") or {}
    return {
        "label": cfg.get("name") or settings.get("name"), "leagueName": settings.get("name"),
        "leagueId": cfg["league_id"], "teamId": my_id, "year": year, "week": week, "matchupPeriod": period,
        "scoringType": (settings.get("scoringSettings") or {}).get("scoringType"),
        "team": {"name": names[my_id], "abbrev": me.get("abbrev"), "record": record_str(me),
                 "seed": me.get("playoffSeed"), "teams": len(teams_raw), "pf": r1(o.get("pointsFor")),
                 "pa": r1(o.get("pointsAgainst")), "pfRank": pf_rank[my_id], "streak": mine_st["streak"],
                 "waiverRank": me.get("waiverRank")},
        "matchup": matchup, "knockout": knockout, "alerts": alerts,
        "lineup": {"starters": starters, "bench": bench, "ir": ir, "advice": advice,
                   "opponent": starters_by_team.get(matchup["opponentId"], []) if matchup else []},
        "season": {"weekly": weekly, "allPlay": mine_st["allPlay"], "allPlayPct": mine_st["allPlayPct"],
                   "expWins": mine_st["expWins"], "luck": mine_st["luck"], "efficiency": mine_st["efficiency"],
                   "benchPts": mine_st["benchPts"], "historyWeeks": len(history), "playoff": playoff,
                   "sos": my_sos and {"avg": my_sos["avg"], "rank": sos_rank.get(my_id), "of": len(sos)},
                   "remaining": remaining},
        "standings": standings, "scoreboard": board, "waivers": waivers, "byes": byes,
        "url": f"https://fantasy.espn.com/football/team?leagueId={cfg['league_id']}&teamId={my_id}&seasonId={year}",
    }


def _remaining_opponents(schedule, my_id, period, last_regular):
    out = []
    for m in sorted(schedule, key=lambda m: m.get("matchupPeriodId", 0)):
        p = m.get("matchupPeriodId", 0)
        if p <= period or p > last_regular or not m.get("away"):
            continue
        h, a = m["home"]["teamId"], m["away"]["teamId"]
        if my_id in (h, a):
            out.append((p, a if h == my_id else h))
    return out


def build_alerts(starters, bench, advice, waivers, byes, week, extra=()):
    alerts = list(extra)
    for s in advice["emptySlots"]:
        alerts.append({"level": "critical", "text": f"Empty {s} slot", "detail": "Nobody is starting there."})
    for r in starters:
        if r["state"] in ("in", "post"):
            continue
        if r["state"] == "bye":
            alerts.append({"level": "critical", "text": f"{r['name']} is on bye", "detail": f"Starting at {r['slot']}."})
        lvl = INJURY_LEVEL.get(r["injury"])
        if lvl:
            alt = max((b for b in bench if r["slotId"] in b["eligible"] and b["state"] == "pre"
                       and b["injury"] in ("ACTIVE", "NORMAL")), key=lambda b: b["proj"] or 0, default=None)
            detail = f"Starting at {r['slot']}."
            if alt:
                detail += f" Best healthy backup: {alt['name']} ({alt['proj'] or 0:.1f} proj)."
            alerts.append({"level": lvl, "text": f"{r['name']} is {r['injuryLabel'].lower()}", "detail": detail})
    for s in advice["swaps"]:
        alerts.append({"level": "serious" if (s["gain"] or 0) >= 3 else "warning",
                       "text": f"Start {s['in']}" + (f" over {s['out']}" if s["out"] else ""),
                       "detail": f"+{s['gain']:.1f} projected ({s['inProj']:.1f} vs {(s['outProj'] or 0):.1f})."})
    for w in waivers[:40]:
        if (w["upgrade"] or 0) >= 2 and w["injury"] in ("ACTIVE", "NORMAL"):
            alerts.append({"level": "info", "text": f"{w['name']} ({w['pos']}, {w['team']}) is available",
                           "detail": f"Projected {w['projAvg']:.1f} a game, {w['upgrade']:.1f} more than {w['replaces']}. {w['status']}."})
            if sum(1 for a in alerts if a["level"] == "info") >= 2:
                break
    if byes and byes[0]["week"] == week + 1:
        hit = [p for p in byes[0]["players"] if p["starter"]]
        if hit:
            alerts.append({"level": "info", "text": f"{len(hit)} starter{'s' if len(hit) > 1 else ''} on bye next week",
                           "detail": ", ".join(f"{p['name']} ({p['pos']})" for p in hit) + "."})
    order = {"critical": 0, "serious": 1, "warning": 2, "info": 3}
    return sorted(alerts, key=lambda a: order[a["level"]])
