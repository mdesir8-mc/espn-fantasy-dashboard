# ESPN Fantasy Dashboard

Public, multi-user dashboard for ESPN fantasy football. Python 3.9+, standard library only. Keep it that way unless
there's a strong reason; it runs with `python3 webapp.py` and nothing else.

## Layout

- `webapp.py`: the web server. Accounts (scrypt hashes, session cookies), per-user league lists in SQLite, rate
  limits, and `/api/data`, which builds each user's dashboards. Routes are in `ROUTES` at the bottom.
- `fantasy_tracker.py`: ESPN/NFL/Sleeper fetches, in-memory and disk caching, and a small CLI (`--print`, `--json`,
  `--snapshot`, `--demo`). `gather(cfg)` fetches one league, `build(cfg, bundle)` turns it into dashboard data.
- `metrics.py`: pure functions that turn raw responses into dashboard data. No network. `build_league(bundle, cfg, now)`.
- `index.html`: the whole front end (vanilla JS, no build step): landing page with sign-in, Leagues page, dashboard.
- `tests/make_fixture.py`: builds a synthetic snapshot (a head-to-head league and a knockout league).
- `DATA_DIR` (default `./data`, `/data` on Railway): `app.db` plus `cache/` (lineup-efficiency history per league
  and a daily slim copy of Sleeper's player list).

## Commands

    python3 webapp.py                                   # http://localhost:8765
    python3 tests/make_fixture.py tests/fixture.json && python3 fantasy_tracker.py --demo tests/fixture.json

To test against real data without hammering ESPN, put leagues in `leagues.json`, take a `--snapshot` and iterate
with `--demo`.

## Deploys

The hosted version is the Railway project `espn-fantasy-dashboard`, service `web`, connected to this repo's `main`
branch: every push to `main` builds the `Dockerfile` and deploys. A volume is mounted at `/data` (`DATA_DIR`), so
accounts survive deploys. Check a deploy with `railway deployment list --service web`.

## Security model (keep it intact)

- ESPN cookies for private leagues never touch the database or logs. The browser stores them in localStorage and
  sends them in the `/api/data` body; `webapp.espn_cfg` builds a per-request config from them.
- In-memory caches of ESPN responses are keyed by `cookie_id(cfg)`, so a private league fetched with one person's
  cookies is never served to someone without them. Any new cached ESPN fetch must include it in its key.
- State-changing requests require `X-Requested-With: fetch` (CSRF guard; cross-site forms can't send it).
  Sessions are `HttpOnly; SameSite=Lax` (and `Secure` when `PORT` is set). Every page escapes ESPN text with `esc()`.
- Users only ever see and delete their own `leagues` rows (`WHERE user_id = ?` on every query).

## Data sources (all unofficial)

- ESPN fantasy API: `https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{id}`
  with views `mTeam, mRoster, mMatchupScore, mSettings, mStatus`. Season-level `?view=proTeamSchedules_wl` gives pro
  teams, bye weeks and game times. Free agents come from `view=kona_player_info` with an `X-Fantasy-Filter` header.
  Past weeks: `view=mBoxscore&view=mMatchupScore&scoringPeriodId=N`.
- ESPN NFL scoreboard: `site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?seasontype=2&week=W&dates=YEAR`
  for game state, clock, red zone and odds. Odds only appear on games that haven't started.
- Sleeper: `/v1/players/nfl/trending/add` joined to `/v1/players/nfl` via `espn_id` (fallback: normalized name + team).

## ESPN quirks found the hard way

- Live matchup points are `totalPointsLive`; `totalPoints` stays 0 until the week is final. ESPN also sends
  `totalProjectedPointsLive` and `winProbability` per side.
- Player stat entries: `statSourceId` 0 = actual, 1 = projected; `statSplitTypeId` 1 = single week, 0 = season.
  Season actual is id `00{year}`; rest-of-season projection is id `10{year}` and its `appliedAverage` is per game.
- Fantasy `proTeamId` matches the ESPN scoreboard team id (LAC = 24, KC = 12). `proTeamId` 0 means free agent.
- Lineup slot ids: 0 QB, 2 RB, 4 WR, 6 TE, 16 D/ST, 17 K, 20 bench, 21 IR, 23 FLEX (full map in `metrics.SLOT_NAMES`).
  `best_lineup` fills strict slots before flex slots, which is optimal for nested flex rules.
- `injuryStatus` is present in `mRoster` but missing from `mBoxscore` player objects.
- Knockout leagues (`scoringType` `WEEKLY_ALL_PLAY_POINTS`) have no home/away pairs: schedule entries carry a
  `teams` array instead. `build_league` hands them to `knockout_block`, which returns `knockout` (`matchup` is null).
  Each week the lowest score among teams still alive is eliminated; `eliminationMatchupPeriod` is the week a team
  went out (0 = alive). On the current week each side has `winProbability` (chance to survive) and
  `eliminationProbabilityStatus` (SAFE/CAUTION/DANGER). Tiebreaks are unknown (`matchupTieRule` is NONE).
- Knockout box scores only include rosters when `mMatchupScore` is requested alongside `mBoxscore`.
- `status.firstScoringPeriod` can be later than 1; earlier weeks are all zeros.
- A 401/403 from the league endpoint means private (or, for a made-up id, nonexistent): ESPN doesn't distinguish.

## Front-end conventions

- Colors are tokens on `:root` with dark-mode overrides; "you" is blue (`--me`), opponent orange (`--opp`). Status
  colors (critical/serious/warning/info) always ship with an icon or label, never color alone.
- Fonts: Barlow for text, Barlow Condensed for numbers and headings.
- Check layout at desktop and 390px width in light and dark; the page must not scroll sideways on mobile.

## Open work

- Only football is supported. Other sports need their own slot/position tables.
- No password reset (no email on purpose).
